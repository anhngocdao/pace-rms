# Architecture Decision Records

This folder holds the decisions behind Pace that are easy to reverse by accident and expensive to reverse on purpose. Each record says what was decided, why, what it costs, and which numbers in METHOD.md, README.md or EXTENDING.md back it. Records are numbered in the order they were written and are never renumbered or deleted; a decision that is later overturned gets a new record that supersedes it, and the old one stays.

## Template

Every record is a short Markdown file, under forty lines, in plain prose, with this shape:

```
# ADR 000N: title
Status: Proposed · Date: YYYY-MM-DD
## Context
## Decision
## Consequences
## Evidence
```

Status is one of Proposed, Accepted, Rejected, or Superseded by ADR 000M. Evidence cites the METHOD.md section by number and heading and repeats the figures exactly as written there, quoting at most one short sentence verbatim. A record may not contain a number that does not appear in the three source documents; if a draft carries one, the record says what the files actually say and notes the discrepancy in one line.

## The rule for new mechanisms

Any new mechanism, a new forecast, a new control, a new plugin seam, a change to what the engine is allowed to see, starts life as an ADR with Status: Proposed, written before the code. It moves to Accepted or Rejected only after there are numbers, a paired backtest, a robustness run or a measured cost, recorded in the Evidence section. A Rejected record is kept in the folder, because a mechanism that did not pay is a result, not an embarrassment; ADR 0006 explains why.
