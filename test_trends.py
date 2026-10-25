from pytrends.request import TrendReq

trends = TrendReq(
    hl="en-US",
    tz=330,
    timeout=(10, 25)
)

keyword = "digital marketing"

trends.build_payload(
    kw_list=[keyword],
    timeframe="today 12-m",
    geo="IN"
)

data = trends.interest_over_time()

if data.empty:
    print("No Google Trends data returned.")
else:
    print(data[[keyword]].tail(12))
    print("Average relative interest:", round(data[keyword].mean(), 2))