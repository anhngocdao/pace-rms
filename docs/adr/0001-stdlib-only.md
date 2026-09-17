# ADR 0001: Standard library only
Status: Accepted · Date: 2026-09-17 (decision made earlier, recorded now)

## Context
Pace is a hotel revenue management engine that has to be read, run and argued with by people who are not going to set up a numerical Python environment first. The natural stack for this kind of work is numpy and pandas, and the natural cost of that stack is an install step, a lockfile, a version matrix and, a year later, a build that no longer resolves. The project's claim is about decision logic, the forecast, the unconstraining and the control layer, not about the toolchain underneath it.

## Decision
The engine uses only the Python standard library. No numpy, no pandas, no third party package of any kind, no lockfile. The floor is Python 3.9 or newer. Every command runs as python3 run.py with no install step, and the plugin seams described in EXTENDING.md load from a folder rather than a registry, so extending the engine does not reintroduce a dependency either.

## Consequences
Anyone can clone the repository and have the full backtest and dashboard in about 45 seconds, and the whole test suite in well under a minute, on a stock interpreter. There is nothing to rot: no pinned versions to drift, no wheel to fail on a new platform. The cost is that everything numerical is written longhand, the normal tail probabilities, the grid search, the linear program dual, and none of it is vectorised. That cost is measured rather than feared: one pricing decision takes 1.22 milliseconds regardless of property size, because the closed form bid price is order of the number of segments, not of the capacity. Where speed mattered, the fix was a better algorithm, not a faster library. The decision also means the engine is the argument. A reader who disagrees with a result can open a single module and see the arithmetic, which is the kind of scrutiny the project asks for.

## Evidence
README.md, opening: "A working hotel revenue management engine, in the Python standard library." The same page states no dependencies, no lockfile, Python 3.9 or newer, and that python3 run.py build takes about 45 seconds with no install step. README.md, Commands: python3 run.py test runs the suite with no install step. METHOD.md section 13, Scale, measured: 1.233 ms per decision at 50 rooms, 1.218 ms at 500 and 1.216 ms at 8,000, between 810 and 822 decisions per second; about 163 seconds of optimizer time per property for a year long horizon; roughly 176 properties per core in an eight hour overnight window, in pure Python with no vectorisation and no parallelism.
