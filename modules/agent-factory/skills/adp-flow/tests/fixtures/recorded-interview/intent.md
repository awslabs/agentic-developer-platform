# Confirmed interview: example delivery

Recorded interview fixture for a fictional Product: delivery home `example/home`; one code story there and two in `example/component`. Three waves, each with a separate human evaluation issue in the home repository. This is supplied test data, not a live interview or an approved plan.

```json
{
  "epic": "epic",
  "title": "Example delivery",
  "description": "Deliver three independently checked example waves.",
  "story_repositories": {
    "story-one": "example/home",
    "story-two": "example/component",
    "story-three": "example/component"
  },
  "waves": [
    {"ref": "w1", "title": "Foundation", "description": "Build the foundation", "stories": ["story-one"], "evaluation": "eval-one"},
    {"ref": "w2", "title": "Integration", "description": "Connect the component", "stories": ["story-two"], "evaluation": "eval-two"},
    {"ref": "w3", "title": "Qualification", "description": "Verify the behavior", "stories": ["story-three"], "evaluation": "eval-three"}
  ]
}
```
