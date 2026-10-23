"""Run SerpApi's official Python client against a fairserp server and assert the results.

Usage: python scripts/compat_check.py http://localhost:8000 YOUR_KEY
Requires `pip install serpapi` and a server with FAIRSERP_SERPAPI_ENGINES=google:auto.
The only change from stock client code is BASE_DOMAIN.
"""

import sys

import serpapi


def main(base_url: str, api_key: str) -> None:
    serpapi.HTTPClient.BASE_DOMAIN = base_url
    client = serpapi.Client(api_key=api_key)

    results = client.search(q="coffee", engine="google")
    meta = results["search_metadata"]
    assert meta["status"] == "Success", meta
    assert results["organic_results"], "no organic results"
    assert meta["json_endpoint"].startswith(base_url), meta["json_endpoint"]
    fair = meta["fairserp"]
    print(f"search: {len(results['organic_results'])} results via {fair['resolved_engine']}, "
          f"providers={[p['engine'] + ':' + p['status'] for p in fair['providers']]}")

    archived = client.search_archive({"search_id": meta["id"]})
    assert archived["search_metadata"]["id"] == meta["id"]
    print("archive: ok")

    account = client.account()
    assert account["this_hour_searches"] >= 1, account
    print(f"account: this_hour_searches={account['this_hour_searches']}")

    try:
        client.search(q="coffee", engine="google", tbs="li:1", fairserp_strict="true")
    except serpapi.HTTPError as exc:
        assert exc.status_code == 400
        print("strict params: rejected unsupported tbs as expected")
    else:
        raise AssertionError("strict params did not reject an unsupported parameter")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
