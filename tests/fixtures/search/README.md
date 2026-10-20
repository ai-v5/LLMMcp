# Search fixtures

Real captures from this machine's network (corporate proxy), reduced to the markup
the parsers actually read. No test using these files touches the network.

| File | Source | Notes |
|---|---|---|
| `google_results.html` | `chrome --headless=new --dump-dom` on Google for `python asyncio tutorial` | 9 `div.MjjYud` blocks with real titles, `<cite>` and snippets. `href` values are Google's opaque `/goto?url=...` redirects, exactly as served. Session-scoped attributes (`id`, `data-ved`, `data-hveid`, `ping`, `nonce`, `csi*`) stripped; inline JS/SVG/icons dropped. |
| `google_gate.html` | Same query over plain HTTP | The JavaScript gate page Google serves without a browser: a `<noscript>` meta-refresh to `/httpservice/retry/enablejs`, plus inline JS that mentions `enablejs` and `hasCaptchaSupport` -- which is why those strings must not be used as block signals. |
| `google_captcha.html` | Synthetic | Minimal `/sorry/index` "unusual traffic" interstitial for block detection. |
| `bing_results.html` | Bing for `python asyncio tutorial` | 10 `li.b_algo` blocks with the real `ck/a?...&u=a1<base64url>` click-tracker hrefs. |
| `bing_no_results.html` | Synthetic | Bing's "no results" list. |
| `bing_unrelated.html` | Bing for `ascend npu mfu` | **Regression capture.** A structurally perfect results page whose content belongs to a different query: this network's Bing answers automated traffic with decoys/spam. Anchors the `BING_UNRELATED` test. |
| `gnews_feed.xml` | Google News RSS for `ascend npu mfu` | 3 real items; RFC-822 `pubDate` values verify the ISO-8601 UTC conversion. |

Referenced by `tests/test_search_google.py`, `tests/test_search_bing.py` and
`tests/test_search_gnews.py`. Rebuild them from `.tmp/probe/html/` with
`.tmp/probe/build_search_fixtures.py` if the live markup changes.
