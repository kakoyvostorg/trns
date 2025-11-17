# Contributing to TRNS

Thank you for your interest in contributing to TRNS! This document provides guidelines and instructions for contributing.

## Code of Conduct

- Be respectful
- Welcome newcomers and help them learn
- Focus on constructive feedback
- Respect different viewpoints and experiences

## How to Contribute

### Reporting Bugs

1. Check if the bug has already been reported in [Issues](https://github.com/yourusername/trns/issues)
2. If not, create a new issue with:
   - Clear title and description
   - Steps to reproduce
   - Expected vs actual behavior
   - Environment details (OS, Python version, etc.)
   - Relevant logs or error messages

### Suggesting Features

1. Check existing issues and discussions
2. Create a new issue with:
   - Clear description of the feature
   - Use case and motivation
   - Proposed implementation (if applicable)

### Pull Requests

1. **Fork the repository**
2. **Create a branch**: `git checkout -b feature/your-feature-name`
3. **Make changes**:
   - Follow the code style (see below)
   - Add tests if applicable
   - Update documentation
4. **Commit changes**: Use clear, descriptive commit messages
5. **Push to your fork**: `git push origin feature/your-feature-name`
6. **Create a Pull Request** with:
   - Clear description of changes
   - Reference to related issues
   - Screenshots (if UI changes)

## Development Setup

1. Clone your fork:
   ```bash
   git clone https://github.com/yourusername/trns.git
   cd trns
   ```

2. Install in development mode:
   ```bash
   pip install -e ".[dev]"
   ```

3. Install pre-commit hooks (optional):
   ```bash
   pre-commit install
   ```

## Code Style

- Follow PEP 8 style guide
- Use type hints where possible
- Write docstrings for functions and classes
- Keep functions focused and small
- Use meaningful variable names

### Formatting

We use `ruff` for code formatting:

```bash
ruff format .
ruff check .
```

### Type Checking

We use `mypy` for type checking:

```bash
mypy src/
```

## Testing

- Write tests for new features
- Ensure all tests pass: `pytest`
- Aim for good test coverage

## Documentation

- Update relevant documentation when adding features
- Keep docstrings up to date
- Add examples for new features
- Update README if needed

## Project Structure

```
trns/
├── src/trns/          # Main package
│   ├── bot/          # Telegram bot
│   ├── transcription/ # Transcription pipeline
│   └── cli/          # CLI interface
├── docs/             # Documentation
├── docker/           # Docker files
├── examples/         # Example scripts
└── tests/            # Test files
```

## Commit Messages

Use clear, descriptive commit messages:

- Start with a verb (Add, Fix, Update, Remove, etc.)
- Be specific about what changed
- Reference issues if applicable

Examples:
- `Add support for Twitter/X.com video links`
- `Fix audio extraction error in Telegram bot`
- `Update documentation for deployment`

## Review Process

1. All PRs require review
2. Address review comments promptly
3. Keep PRs focused (one feature/fix per PR)
4. Ensure CI checks pass

## Questions?

Feel free to open an issue for questions or discussions!

