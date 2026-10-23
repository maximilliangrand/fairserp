"""Run SerpApi's official Python client against a fairserp server.

Usage: python scripts/compat_check.py http://localhost:8000 YOUR_KEY
Requires `pip install serpapi`. The only change from stock client code is BASE_DOMAIN.
"""

import sys

import serpapi


def main(base_url: str, api_key: str) -> None:
    serpapi.HTTPClient.BASE_DOMAIN = base_url
    client = serpapi.Client(api_key=api_key)

    results = client.search(q="coffee", engine="google")
    organic = results["organic_results"]
    print(f"search: status={results['search_metadata']['status']} organic={len(organic)}")
    print(f"  #1 {organic[0]['title']} | {organic[0]['link']}")

    archived = client.search_archive({"search_id": results["search_metadata"]["id"]})
    print(f"archive: status={archived['search_metadata']['status']}")

    page_two = results.next_page()
    if page_two is not None:
        page_number = page_two["search_information"].get("page_number")
        print(f"next_page: page_number={page_number} organic={len(page_two['organic_results'])}")

    account = client.account()
    print(f"account: this_hour_searches={account['this_hour_searches']}")

    location = client.locations(q="Austin", limit=1)[0]
    print(f"locations: {location['canonical_name']} gps={location['gps']}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
