from urllib.request import Request, urlopen


URL = "https://www.viagogo.com/bg/Concert-Tickets/E-161267796"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
}


def main():
    print("Checking Viagogo...")

    request = Request(URL, headers=HEADERS)

    try:
        with urlopen(request, timeout=30) as response:
            content = response.read().decode("utf-8", errors="ignore")

            print("HTTP status:", response.status)
            print("Page size:", len(content))

            if response.status == 200:
                print("Successfully accessed the event page.")

                with open(
                    "viagogo_response.html",
                    "w",
                    encoding="utf-8"
                ) as file:
                    file.write(content)

                print("Saved response to viagogo_response.html")
            else:
                print("Could not access the page.")

    except Exception as e:
        print("Error accessing Viagogo:")
        print(type(e).__name__, e)


if __name__ == "__main__":
    main()