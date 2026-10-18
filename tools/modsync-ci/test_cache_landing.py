import importlib.util
from pathlib import Path
import types
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("cache", Path(__file__).with_name("cache.py"))
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)

refresh_spec = importlib.util.spec_from_file_location("refresh_archives", Path(__file__).with_name("refresh_archives.py"))
refresh_archives = importlib.util.module_from_spec(refresh_spec)
refresh_spec.loader.exec_module(refresh_archives)

PAGE_URL = "https://deadlystream.com/files/file/123-example-post/"
WANTED = "Example Pack (2x).7z"


def item():
    kind, direct = cache.provider(PAGE_URL)
    return {"id": cache.key_for(PAGE_URL), "url": PAGE_URL, "provider": kind, "direct": direct, "guides": []}


def args(retries=2):
    return types.SimpleNamespace(retries=retries, timeout=5)


def row(name, number):
    return ('<li class="ipsDataItem"><div class="ipsDataItem_main"><h4 class="ipsDataItem_title">'
            '<a href="/files/file/123-example-post/?do=download&amp;r=%d&amp;confirm=1&amp;t=1&amp;csrfKey=key">%s</a>'
            '</h4></div></li>' % (number, name))


GOOD = "<html><body><ul>%s%s</ul></body></html>" % (row(WANTED, 1001), row("Other Pack.7z", 1002))
EMPTY = "<html><body><p>Example post</p></body></html>"
AMBIGUOUS = ('<html><body><a href="/attachment.php?id=1">one</a>'
             '<a href="/attachment.php?id=2">two</a></body></html>')
CHALLENGE = "<html><head><title>Just a moment...</title></head><body></body></html>"


class Page:
    def __init__(self, source, content_type="text/html; charset=utf-8"):
        self.source = source.encode()
        self.headers = {"Content-Type": content_type}

    def geturl(self):
        return PAGE_URL

    def read(self, size=-1):
        return self.source

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Site:
    """Serves the queued landing pages in order; an exception in the queue is raised as a failed fetch."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = 0

    def __call__(self, url, request_args, headers=None):
        self.requests += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return Page(response) if isinstance(response, str) else response


class LandingRetryTests(unittest.TestCase):
    def resolve(self, site, retries=2, selections=None):
        selections = {"123": WANTED} if selections is None else selections
        with mock.patch.object(cache, "request", site), \
                mock.patch.object(cache, "load_selections", return_value=selections), \
                mock.patch.object(cache.time, "sleep") as sleep:
            try:
                return cache.resolve(item(), args(retries)), sleep
            except ValueError as error:
                return error, sleep

    def test_page_without_downloads_is_fetched_again_and_then_resolves(self):
        site = Site(EMPTY, GOOD)
        endpoint, sleep = self.resolve(site)
        self.assertIsInstance(endpoint, str)
        self.assertIn("r=1001", endpoint)
        self.assertEqual(site.requests, 2)
        sleep.assert_called_once_with(cache.PAGE_RETRY_SECONDS)

    def test_ambiguous_then_empty_then_good_page_resolves(self):
        site = Site(AMBIGUOUS, EMPTY, GOOD)
        endpoint, sleep = self.resolve(site)
        self.assertIn("r=1001", endpoint)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         [cache.PAGE_RETRY_SECONDS, cache.PAGE_RETRY_SECONDS * 2])

    def test_page_that_stays_empty_fails_with_page_error_after_bounded_attempts(self):
        site = Site(EMPTY, EMPTY, EMPTY, GOOD)
        error, sleep = self.resolve(site, retries=2)
        self.assertEqual(str(error), cache.PAGE_UNAVAILABLE)
        self.assertEqual(site.requests, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn("no unique archive endpoint", str(error))
        self.assertNotIn("reviewed archive selection", str(error))

    def test_no_retries_means_one_fetch_and_no_sleep(self):
        site = Site(EMPTY, GOOD)
        error, sleep = self.resolve(site, retries=0)
        self.assertEqual(str(error), cache.PAGE_UNAVAILABLE)
        self.assertEqual(site.requests, 1)
        sleep.assert_not_called()

    def test_persistently_ambiguous_page_keeps_adapter_reason(self):
        site = Site(AMBIGUOUS, AMBIGUOUS, AMBIGUOUS)
        error, _ = self.resolve(site)
        self.assertIn("no unique archive endpoint", str(error))
        self.assertEqual(site.requests, 3)

    def test_failed_fetch_keeps_its_own_reason_and_is_not_page_retried(self):
        site = Site(ValueError("HTTP 503"), GOOD)
        error, sleep = self.resolve(site)
        self.assertEqual(str(error), "HTTP 503")
        self.assertEqual(site.requests, 1)
        sleep.assert_not_called()

    def test_selection_mismatch_is_not_retried_and_names_the_selection(self):
        site = Site(GOOD, GOOD)
        error, sleep = self.resolve(site, selections={"123": "Renamed Pack.7z"})
        self.assertIn("reviewed archive selection for file 123 matches 0", str(error))
        self.assertEqual(site.requests, 1)
        sleep.assert_not_called()

    def test_challenge_page_is_never_retried(self):
        site = Site(CHALLENGE, GOOD)
        error, sleep = self.resolve(site)
        self.assertIn("challenge/CAPTCHA", str(error))
        self.assertEqual(site.requests, 1)
        sleep.assert_not_called()

    def test_non_html_landing_is_not_retried(self):
        site = Site(Page("PK", content_type="application/zip"), GOOD)
        error, _ = self.resolve(site)
        self.assertEqual(str(error), "landing URL has no explicit archive endpoint")
        self.assertEqual(site.requests, 1)

    def test_backoff_is_capped(self):
        site = Site(*[EMPTY] * 6)
        _, sleep = self.resolve(site, retries=5)
        delays = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(len(delays), 5)
        self.assertEqual(max(delays), cache.PAGE_RETRY_CAP_SECONDS)


class PageReasonClassTests(unittest.TestCase):
    def test_page_reason_is_transient_not_adapter_or_selection(self):
        self.assertEqual(refresh_archives.classify(cache.PAGE_UNAVAILABLE), ("network", None))
        self.assertNotIn(cache.PAGE_UNAVAILABLE, refresh_archives.ADAPTER_REASONS)


if __name__ == "__main__":
    unittest.main()
