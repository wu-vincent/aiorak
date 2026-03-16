# Contributing to aiorak

Thank you for your interest in contributing to aiorak!

## Development setup

1. Clone the repository:
   ```bash
   git clone https://github.com/wu-vincent/aiorak.git
   cd aiorak
   ```

2. Install in development mode:
   ```bash
   pip install -e ".[dev]"
   ```

## Running tests

```bash
# Full test suite (parallelized)
python -m pytest -n auto

# Single test file
python -m pytest tests/test_reliability.py

# With coverage
python -m pytest --cov --cov-report=term-missing
```

## Code style

We use [Ruff](https://docs.astral.sh/ruff/) for linting and formatting (line length: 120).

```bash
# Check
ruff check src/ tests/
ruff format --check src/ tests/

# Auto-fix
ruff check --fix src/ tests/
ruff format src/ tests/
```

## Pull request process

1. Fork the repository and create a feature branch.
2. Make your changes with tests for new functionality.
3. Ensure `ruff check` and `ruff format --check` pass.
4. Ensure `python -m pytest -n auto` passes.
5. Open a pull request with a clear description of the change.
