"""Read native prevention independently of inbound incident triggers."""

from datetime import UTC, datetime

from l5_services.adapters.devops.spaces import SpaceIdentityError, SpaceSelection


class PreventionAdapter:
    def __init__(self, client, *, account_id: str, region: str):
        self.client = client
        self.account_id = account_id
        self.region = region

    def read(self, selection: SpaceSelection, agent_space_id: str,
             configuration_revision: int) -> dict:
        fingerprint = selection.fingerprint()
        if ((selection.account_id, selection.region) != (self.account_id, self.region)
                or not isinstance(agent_space_id, str) or not agent_space_id):
            raise SpaceIdentityError("prevention readback target differs from selected binding")
        response = self.client.get_agent_space(agentSpaceId=agent_space_id)
        if (response.get("agentSpace") or {}).get("agentSpaceId") != agent_space_id:
            raise SpaceIdentityError("prevention readback belongs to another native space")
        goals = []
        token = None
        seen = set()
        for _ in range(10):
            page = self.client.list_goals(agentSpaceId=agent_space_id, limit=100,
                                          **({"nextToken": token} if token else {}))
            if not isinstance(page.get("goals"), list) or len(page["goals"]) > 100:
                raise ValueError("native goal readback exceeded page bounds")
            for goal in page["goals"]:
                schedule = goal.get("evaluationSchedule")
                if (not isinstance(goal.get("goalId"), str) or not goal["goalId"]
                        or goal.get("goalType") not in {"ONCALL_REPORT", "CUSTOMER_DEFINED"}
                        or not isinstance(schedule, dict)
                        or schedule.get("state") not in {"ENABLED", "DISABLED"}
                        or not isinstance(schedule.get("expression"), str)
                        or not schedule["expression"]
                        or goal["goalId"] in seen):
                    raise ValueError("native goal schedule cannot be qualified")
                seen.add(goal["goalId"])
                goals.append({"goal_id": goal["goalId"], "goal_type": goal["goalType"],
                              "state": schedule["state"], "expression": schedule["expression"],
                              "last_task_id": goal.get("lastTaskId")})
            token = page.get("nextToken")
            if not token:
                break
            if not isinstance(token, str) or token in seen:
                raise ValueError("native goal pagination repeated a cursor")
            seen.add(token)
        else:
            raise ValueError("native goal readback exceeded page limit")
        return {"scope_fingerprint": fingerprint, "configuration_revision": configuration_revision,
                "native_space_id": agent_space_id, "observed_at": datetime.now(UTC).isoformat(),
                "native_schedule": ({"enabled": any(goal["state"] == "ENABLED" for goal in goals),
                                     "goals": goals} if goals else None)}

    def set_enabled(self, selection: SpaceSelection, agent_space_id: str, configuration_revision: int,
                    *, goal_id: str, expression: str, enabled: bool, operation_id: str) -> dict:
        observed = self.read(selection, agent_space_id, configuration_revision)
        schedule = observed["native_schedule"]
        if schedule is None:
            raise ValueError("native prevention schedule has not appeared")
        matches = [goal for goal in schedule["goals"] if goal["goal_id"] == goal_id]
        if len(matches) != 1 or matches[0]["expression"] != expression:
            raise ValueError("selected native goal or cadence differs from project policy")
        if enabled and any(goal["state"] == "ENABLED" and goal["goal_id"] != goal_id
                           for goal in schedule["goals"]):
            raise ValueError("another native goal schedule is already enabled")
        target = "ENABLED" if enabled else "DISABLED"
        if matches[0]["state"] != target:
            self.client.update_goal(agentSpaceId=agent_space_id, goalId=goal_id,
                                    evaluationSchedule={"state": target}, clientToken=operation_id)
        result = self.read(selection, agent_space_id, configuration_revision)
        goals = (result["native_schedule"] or {}).get("goals", [])
        if not any(goal["goal_id"] == goal_id and goal["state"] == target
                   and goal["expression"] == expression for goal in goals):
            raise ValueError("native prevention state or cadence was not confirmed")
        return result
