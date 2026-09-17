# Pace, working notes for the assistant

Pace is a hotel revenue management engine written in the Python standard
library. It builds a market, learns from the bookings that market produces,
and prices every future night with the reasoning attached. README.md is the
front door, METHOD.md is the maths, EXTENDING.md is how to add to it.

## Commands

```
python3 run.py test            # unit tests + golden numbers, under a minute
python3 run.py build --quick   # one month backtest, about 15 seconds
python3 run.py build           # full backtest + dashboard, about 45 seconds
PACE_FULL=1 python3 run.py test   # also checks the full-backtest golden row
python3 run.py show 2025-03-15 # one night, with its explanation
python3 run.py robustness      # four markets, same engine
python3 run.py experiment      # randomised rate experiment (null result)
python3 run.py network --quick # network model with LOS and room types
python3 run.py bench           # milliseconds per decision
```

## Hard constraints

- Standard library only. No numpy, no pandas, no requests, no lockfile.
  `tests/test_golden.py` scans every import in `pace/` and `plugins/`.
- Python 3.9 or newer. No `match`, no `X | Y` unions, no `tomllib`.
- The market emits requests, not bookings. The engine sees only the ledger.
- Do not add machine learning. The point of the project is the control layer.
- Negative results stay published (METHOD.md sections 12 and 14).

## Golden numbers

Every backtest is seeded, so these numbers are exact until someone changes a
mechanism. `run.py test` fails when they move.

| Build | Static | Ladder | Engine | Engine vs ladder |
|---|---|---|---|---|
| quick (`--quick`, 31 nights) | 64.50 | 78.80 | 82.98 | +5.3% RevPAR |
| full (184 nights) | 134.64 | 143.04 | 152.25 | +6.4% RevPAR, +5.5% GOPPAR |

RevPAR in the currency of the scenario. Full-build detail lives in README.md
(the table under "What it finds") and METHOD.md section 13.

**Rule for changing a golden number.** A change that moves a number is either
a bug fix or a new mechanism. Either way, the same commit updates this table,
`tests/test_golden.py`, and the matching README.md and METHOD.md rows, and the
commit message says why the number moved. A number never moves silently.

## Sources of truth

- `METHOD.md` explains every mechanism and cites the papers. If code and
  METHOD.md disagree, one of them is wrong; fix that before adding anything.
- `EXTENDING.md` is the only supported way to add signals and rate rules
  (drop a file in `plugins/`). Do not add extension points elsewhere.
- `pace/levels.py` holds the four explanation layers for every recommendation.
  A new mechanism is not finished until it can explain itself there.
- `docs/adr/` records decisions. A new mechanism starts as an ADR marked
  "Proposed" before any code lands.

## Ritual for every change

1. Branch from `main`. Never commit to `main` directly.
2. Write or extend a test first (`tests/test_core.py`, `tests/test_network.py`,
   or `tests/test_golden.py`). Watch it fail.
3. Make it pass with the smallest change that keeps the code readable.
4. `python3 run.py test` and `python3 run.py build --quick`. Compare the
   summary with the golden table above.
5. If a number moved on purpose, follow the golden-number rule.
6. Keep README.md, METHOD.md and EXTENDING.md in step with the code in the
   same commit. Docs that drift are worse than no docs.
7. Commit with the voice below. CI (`.github/workflows/check.yml`) reruns
   step 4 on Python 3.9 and 3.12; the full golden row runs on `main`.

## Commit voice

One sentence with a verb, in the register of the existing log: say what is
now true, not what was done. `git log --oneline` shows the pattern.

- Yes: "A cell sitting in the Wednesday column does not need to say Wednesday"
- No: "fix dashboard", "update tests", "wip"

## Things that look like bugs and are not

- The randomised rate experiment shows a small negative lift. That is the
  published result (METHOD.md section 12), not a regression.
- The network model does not beat the single-night engine without length of
  stay and room types (METHOD.md section 14). Also on purpose.
- `run.py test` writes nothing under `out/`; the golden tests run in a
  temporary directory so a full build's dashboard survives a test run.
