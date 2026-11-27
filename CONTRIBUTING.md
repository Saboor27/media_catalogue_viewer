# Contributing

Thanks for your interest! Bug reports, rules files for public sites and pull requests are welcome.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest
```

The tests run offline against a local mock shop (`tests/mock_site.py`); no browser or internet is needed.

## Guidelines

- Keep the scraper and viewer **free of site-specific code**. Anything about one website belongs in a rules file.
- Add or update a test for every behaviour change; `pytest` must pass before you open a pull request.
- The core must stay dependency-light (`beautifulsoup4`, `lxml`); Selenium and PyYAML stay optional.
- Write for Python 3.9+.
- **Rules files:** only contribute rules for sites that explicitly allow scraping (or public practice sandboxes),
  and make sure the rules respect the site's `robots.txt`. Do not commit scraped data, and do not add rules for
  private or adult sites to this repository.

## Reporting a problem

Please include the command you ran, your Python version, the output of `--test` and, if it is about a rules file,
the (non-private) file itself.
