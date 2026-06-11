# Project Instructions: CapIQReplacement

## Strategic Directives
- **Performance First**: Optimize all data processing scripts for high throughput.
- **Strict Typing**: Use Python type hints throughout the codebase.
- **Architectural Pattern**: Follow the established pattern in `app/core/` for new modules.
- **Documentation**: All new functions must include Google-style docstrings.

## Pro Workflow
- **Plan Mode**: Always research and propose a plan before editing more than 2 files.
- **Validation**: Run existing tests in `tests/` after any structural changes.
- **Security**: Never hardcode API keys; use `.env` files.

## Specialized Knowledge
- **CapIQ Context**: We are replacing Capital IQ functionalities with open-source and EDGAR-based tools.
- **Data Sources**: Prioritize `edgartools` and `yfinance` for financial data extraction.
- **Database**: Use SQLAlchemy for all database interactions.
