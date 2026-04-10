# Requirements Analysis Plan

## Project Context
- **Issue**: #[NUMBER]
- **Title**: [TITLE]
- **Created**: [DATE]

## Initial Request
[Copy of original issue body]

---

## Research Findings

### External Research
[Agent will populate with findings from web search]

### Internal Research
[Agent will populate with findings from codebase analysis]

---

## Questions

Please answer the following questions to help define requirements. Fill in the `[Answer]:` sections.

### Business Context

#### 1. Business Goals
What is the primary business objective for this project?
- A) Increase efficiency/automation
- B) Enable new capabilities
- C) Improve user experience
- D) Reduce costs
- E) Compliance/security requirement
- F) Other (please describe)

[Answer]:

#### 2. Success Criteria
How will we measure success? What metrics matter?

[Answer]:

#### 3. Timeline
Is there a deadline or target date?
- A) Flexible (no hard deadline)
- B) Soft deadline: [date]
- C) Hard deadline: [date]
- D) ASAP

[Answer]:

---

### User Context

#### 4. Target Users
Who are the primary users of this system?
- A) Internal team members
- B) External customers
- C) Administrators/operators
- D) API consumers/developers
- E) Multiple user types (describe below)

[Answer]:

#### 5. User Scale
What is the expected number of users?
- A) Small (<50 users)
- B) Medium (50-500 users)
- C) Large (500-5000 users)
- D) Enterprise (5000+ users)

[Answer]:

#### 6. User Technical Level
What is the technical level of primary users?
- A) Non-technical (needs simple UI)
- B) Somewhat technical (comfortable with basic tools)
- C) Technical (can use CLI, APIs)
- D) Expert (developers, DevOps)

[Answer]:

---

### Functional Requirements

#### 7. Core Features
What are the must-have features? (List top 3-5)

[Answer]:

#### 8. Nice-to-Have Features
What features would be good to have but aren't critical?

[Answer]:

#### 9. Out of Scope
What is explicitly NOT part of this project?

[Answer]:

#### 10. Integration Requirements
What systems must this integrate with?

[Answer]:

#### 11. Data Requirements
What data will be processed/stored? Any sensitivity concerns?

[Answer]:

---

### Non-Functional Requirements

#### 12. Performance
What are the performance expectations?
- A) Best effort (no strict requirements)
- B) Standard (sub-second response for most operations)
- C) High performance (millisecond response times)
- D) Real-time (streaming/live data)

[Answer]:

#### 13. Availability
What uptime is required?
- A) Business hours only
- B) 99% (~3.5 days downtime/year)
- C) 99.9% (~8.7 hours downtime/year)
- D) 99.99% (~52 minutes downtime/year)

[Answer]:

#### 14. Security
What security requirements apply?
- A) Basic (authentication only)
- B) Standard (auth + encryption at rest/transit)
- C) High (compliance requirements, audit logging)
- D) Regulated (SOC2, HIPAA, PCI-DSS, etc.)

[Answer]:

#### 15. Scalability
How should the system scale?
- A) Fixed capacity (known, stable load)
- B) Manual scaling (operators scale as needed)
- C) Auto-scaling (automatic based on load)
- D) Elastic (handle unpredictable spikes)

[Answer]:

---

### Technical Context

#### 16. Existing Infrastructure
What infrastructure already exists that should be used?

[Answer]:

#### 17. Technology Preferences
Are there required or preferred technologies? Any technologies to avoid?

[Answer]:

#### 18. Constraints
What constraints must be respected? (budget, team skills, existing systems)

[Answer]:

---

## Next Steps

After filling in all answers:
1. Save this file
2. Add the `aidlc-continue` label to the issue
3. @agent-pm will validate answers and generate requirements document

---

## For Agent Use

### Validation Checklist
- [ ] All [Answer]: tags filled
- [ ] No ambiguous responses
- [ ] No contradictions
- [ ] Sufficient detail for requirements document
