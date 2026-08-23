import requests

URL = "https://www.viagogo.com/bg/Concert-Tickets/E-161267796"

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}


def main():
    print("Checking Viagogo...")

    response = requests.get(
        URL,
        headers=headers,
        timeout=30
    )

    print("HTTP status:", response.status_code)
    print("Page size:", len(response.text))

    if response.status_code == 200:
        print("Successfully accessed the event page.")

        # Save the response so we can inspect what Viagogo
        # actually sends to the GitHub runner.
        with open("viagogo_response.html", "w", encoding="utf-8") as f:
            f.write(response.text)

        print("Saved response to viagogo_response.html")
    else:
        print("Could not access the page.")


if __name__ == "__main__":
    main()