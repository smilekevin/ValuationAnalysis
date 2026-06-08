from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    src_dir = project_root / "src"
    sys.path.insert(0, str(src_dir))

    from valuation_analysis.providers.sec_companyfacts import SecCompanyFactsProvider

    provider = SecCompanyFactsProvider()

    last_line_length = 0

    def show_progress(message: str) -> None:
        nonlocal last_line_length
        padded_message = message.ljust(last_line_length)
        sys.stdout.write(f"\r{padded_message}")
        sys.stdout.flush()
        last_line_length = len(message)

    index = provider.refresh_bulk_sic_index(progress_callback=show_progress)
    cache_path = provider.get_bulk_sic_index_path()

    company_count = sum(len(symbols) for symbols in index.values())
    sys.stdout.write("\n")
    sys.stdout.flush()
    print(f"Built SEC SIC index with {len(index)} SIC groups and {company_count} ticker mappings.")
    print(f"Cache written to: {cache_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
