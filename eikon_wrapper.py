import time
from datetime import datetime
from math import ceil, floor

import eikon as ek
import numpy as np
import pandas as pd
from more_itertools import chunked
from tqdm.auto import tqdm


def get_timeseries_wrapped(tickers, fields, params):
    df = ek.get_timeseries(
        tickers,
        fields,
        start_date=params.get("SDate"),
        end_date=params.get("EDate"),
        interval=params.get("Frq", "daily"),
    )

    df["ticker"] = tickers[0]

    return df, None


def _decide_get_function(fields, params):

    timeseries_fields = [
        "TIMESTAMP",
        "VALUE",
        "VOLUME",
        "HIGH",
        "LOW",
        "OPEN",
        "CLOSE",
        "COUNT",
    ]

    if isinstance(fields, str):
        fields = [fields]

    if fields is None or all([field in timeseries_fields for field in fields]):
        return get_timeseries_wrapped

    if "Frq" in params.keys():
        if params["Frq"] in ["tick", "minute", "hour"]:
            raise ValueError(
                "You requested data of ek.get_data() requesting timeperiods it does not support"
            )

    return ek.get_data


def correct_fields(fields):
    if fields is None or len(fields) == 0:
        return None
    return fields


def correct_tickers(tickers):
    if isinstance(tickers, str):
        return [tickers]
    return tickers


def correct_params(chosen_function, params):
    correction = {"start_date": "SDate", "end_date": "EDate", "interval": "Frq"}
    if chosen_function == ek.get_timeseries:
        for correction_name, actual_name in correction.items():
            if actual_name in params:
                params[correction_name] = params[actual_name]


def handle_and_update_server_side_problem(server_side_problems, err):
    server_side_problems += 1
    print(err.message)
    seconds_to_wait = wait_time(server_side_problems)
    print(f"waiting {seconds_to_wait} second(s) before new pull request...")
    time.sleep(wait_time(seconds_to_wait))


def _get_many_params(params, date_range, chosen_function):
    if date_range is not None:
        params_copies = []
        for i in range(len(date_range) - 1):
            new_params = params.copy()
            new_params["SDate"] = date_range[i].strftime("%Y-%m-%d")
            new_params["EDate"] = date_range[i + 1].strftime("%Y-%m-%d")
            correct_params(new_params, chosen_function)
            params_copies.append(new_params)
        return params_copies
    return [params]


def register_error(d, tickers, err):
    for ticker in tickers:
        d[ticker] = err


def get_data_and_handle_errors(getting_function, tickers, fields, params):
    ticker_and_error = {}

    server_side_problems = 0

    while server_side_problems < 5:

        try:
            # if there is only one ticker causing the error should get_and_log perhaps split call into separate tickers?
            # fields not avalible should (and I think are) set to NA
            new_df, err = getting_function(tickers, fields, params)

            return new_df, pd.DataFrame.from_dict({}, columns=["error"], orient="index")

        except ek.EikonError as err:
            register_error(ticker_and_error, tickers, err)
            if err.code == -1:
                # Data missing or something else bad
                return (
                    None,
                    pd.DataFrame.from_dict(
                        ticker_and_error, columns=["error"], orient="index"
                    ),
                )

            if err.code == 408:
                # HTTP Timeout exception, this just happens frequently... think it can be server side problem
                handle_and_update_server_side_problem(server_side_problems, err)

            if (
                err.code == 400 or err.code == 2504
            ):  # Backend error, Eikon suggests waiting this one out
                handle_and_update_server_side_problem(server_side_problems, err)

            if err.code == 429:
                print(err.message)
                print(
                    "If this error message keeps happening, your subscription has probably collected too much data today"
                )
                # Either daily limit is reached or call based limit reached latter can be a problem from get_datas

    # Can only end up here if the while loops breaks without having returned --> there is an error
    return (
        None,
        pd.DataFrame.from_dict(ticker_and_error, columns=["error"], orient="index"),
    )


def _decide_tickers_and_params(chosen_function, fields, lst_of_tickers, params):

    data_limit = {get_timeseries_wrapped: 2_500, ek.get_data: 9_000}
    date_range = None
    tickers_params_pairs = []

    # If there this is not a timeseries problem
    if "SDate" not in params.keys():
        return [(lst_of_tickers, params)]

    for ticker in lst_of_tickers:
        df, err = get_data_and_handle_errors(
            chosen_function, ticker, fields if fields is None else fields[0], params
        )
        if df is not None:
            break
    else:
        raise ValueError("Not able to get data for any of the chosen tickers")

    fields = ["index"] + df.columns.to_list()

    number_of_timepoints = df.shape[0]
    print(f"[INFO] Number of dates: {number_of_timepoints}")

    number_of_tickers_at_once = floor(
        data_limit[chosen_function] / (len(fields) * number_of_timepoints)
    )
    print(
        f"[INFO] Number of tickers at once: {min(max(number_of_tickers_at_once, 1), len(lst_of_tickers))}"
    )

    if number_of_tickers_at_once < 1:
        number_of_timepoints_at_once = floor(data_limit[chosen_function] / len(fields))
        print(f"[INFO] Max time window length: {number_of_timepoints_at_once}")
        intervals_needed = ceil(number_of_timepoints / number_of_timepoints_at_once)

        time_delta = (
            pd.to_datetime(params["EDate"]) - pd.to_datetime(params["SDate"])
        ) / intervals_needed
        date_range = [
            pd.to_datetime(params["SDate"]) + i * time_delta
            for i in range(intervals_needed - 1)
        ] + [pd.to_datetime(params["EDate"])]
        number_of_tickers_at_once = 1

    if chosen_function is get_timeseries_wrapped:
        number_of_tickers_at_once = 1

    sub_ticker_lst = list(chunked(lst_of_tickers, number_of_tickers_at_once))
    params_copies = _get_many_params(params, date_range, chosen_function)

    for sub_tickers in sub_ticker_lst:
        for params_copy in params_copies:
            tickers_params_pairs.append((sub_tickers, params_copy))

    return tickers_params_pairs


# Function to maximize data per http request.
def _divide_pull_request(lst_of_tickers, fields, params):
    # batch, deduce get_data or get_timeseries size given
    # lst_of_tickers x fields x (params[start]-params[end])*freq < 10,000 or 3,000
    # also adjust params so that they fit selected function
    tickers = correct_tickers(lst_of_tickers)
    chosen_function = _decide_get_function(fields, params)
    fields = correct_fields(fields)
    correct_params(chosen_function, params)
    tickers_params_pairs = _decide_tickers_and_params(
        chosen_function, fields, tickers, params
    )  # TODO

    return tickers_params_pairs, chosen_function, fields


def wait_time(server_side_problems):
    return 3 ** (server_side_problems - 1)


def save_if_criteria_met(df, filename, count):
    if (not count % 10 or count == -1) and filename is not None:
        df.to_csv(filename)


def format_filename(filename):
    if filename is None:
        return None

    if filename[-4:] == ".csv":
        return filename
    return filename + ".csv"


def format_error_filename(filename):
    if filename is None:
        return None

    if filename[-4:] == ".csv":
        return filename[:-4] + "_errors.csv"
    return filename + "_errors.csv"


def df_append(old, new):
    if old is None:
        return new

    if new is None:
        return old

    return pd.concat([old, new])


def get_data(lst_of_tickers, fields, params, filename=None):
    # (return or save) requested data and (return or save) non-retreived-data

    saved_df = None
    error_df = None

    filename = format_filename(filename)
    error_filename = format_error_filename(filename)

    tickers_params_pairs, chosen_function, fields = _divide_pull_request(
        lst_of_tickers, fields, params
    )

    for i, (tickers, param) in enumerate(tqdm(tickers_params_pairs, desc="Ticker")):

        new_df, ticker_and_error = get_data_and_handle_errors(
            chosen_function, tickers, fields, param
        )

        # Merge new data with the one we've already fetched
        saved_df = df_append(saved_df, new_df)
        error_df = df_append(error_df, ticker_and_error)

        # In case user has requested writing to file -> log result as it stands
        save_if_criteria_met(saved_df, filename, i)
        save_if_criteria_met(error_df, error_filename, i)

    # In case user has requested writing to file -> log final df result
    save_if_criteria_met(saved_df, filename, -1)
    save_if_criteria_met(error_df, error_filename, -1)

    return saved_df, error_df
