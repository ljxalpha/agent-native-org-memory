Distill the following memory records into team knowledge products. Return exactly
one JSON object with top-level `products`.

Each product must include:
- title
- category
- summary
- evidence
- citations as objects with memory_id and revision_hash

Optional product fields:
- topic_key
- confidence
- conflicts

Configured categories:
{{ categories_json }}

Category evidence rules:
{{ category_evidence_rules_json }}

Accepted response schema:
{{ response_schema_json }}

Records:
{{ records_json }}
