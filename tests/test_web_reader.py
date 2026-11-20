"""The Web Reader agent's program, reading from a loopback web
server that holds fictional pages for Harbourline Studio. Loopback is
only reachable because the tests set the test-only variable; the
safety tests prove what stays refused with and without it.
"""

import asyncio
import re
import time

import pytest
from standard.http import FILE_MOST

from tests.harness import Chat, Records, probe, run, way_out
from tests.conftest import declaring
from tests.web_stub import LOOPBACK_VARIABLE, WebStub, text_pdf, web  # noqa: F401
PRICING = """<!doctype html>
<html><head><title>Harbourline Studio — Pricing</title>
<script>window.tracking = "Starter plan costs nothing";</script>
<style>.plan { color: red }</style></head>
<body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<header><h1>Pricing</h1></header>
<main>
<p>Every plan includes unlimited projects and email support.</p>
<h2>Starter</h2>
<p>The Starter plan costs USD 12 a month
   for one designer.</p>
<ul><li>5 GB storage</li><li>Shared <a href="/templates">templates</a></li></ul>
<h2>Refunds</h2>
<p>Annual plans can be refunded within 30 days of purchase.</p>
<p>See the <a href="https://help.harbourline.example/refunds#annual">refund guide</a>.</p>
</main>
<footer>Copyright Harbourline Studio. <a href="/privacy">Privacy</a></footer>
</body></html>"""

REPORT_PAGES = [
    ["Harbourline Studio Annual Report 2026", "Revenue grew to USD 1.2 million."],
    ["Outlook", "The studio plans to open a second office in Lisbon."],
    ["Risks", "Currency movements may reduce margins."],
]


def make():
    provider = Records()
    return Chat(provider), provider


def invoke(agents, ex, name, inputs, chat_level=2):
    return run(ex.invoke(agents["web_reader"], name, inputs, chat_level=chat_level))


class TestThePackage:
    def test_its_ready_names_every_declared_function(self, agents):
        agent = agents["web_reader"]
        assert probe(agent) == []

    def test_every_function_only_reads(self, agents):
        """A copy handed to the chat is a download: level 0, like reading."""
        levels = {name: agents["web_reader"].function(f"pages_{name}")["level"]
                  for name in ("read", "find", "save")}
        assert levels == {"read": 0, "find": 0, "save": 0}


class TestReading:
    def test_a_web_page_reads_as_sections_without_its_boilerplate(self, agents, web):
        web.page("/pricing", PRICING)
        start = web.redirect("/plans", "/pricing", status=301)
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": start})
        assert status == "success", result
        assert result["final_url"] == web.url + "/pricing"
        assert result["title"] == "Harbourline Studio — Pricing"
        assert (result["kind"], result["unit"]) == ("html", "section")
        headings = [p["heading"] for p in result["parts"]]
        assert headings == ["# Pricing", "## Starter", "## Refunds"]
        starter = result["parts"][1]["text"]
        # Whitespace collapsed, list items kept as lines.
        assert "The Starter plan costs USD 12 a month for one designer." in starter
        assert "- 5 GB storage" in starter
        everything = " ".join(p["text"] for p in result["parts"])
        assert "tracking" not in everything and "color: red" not in everything
        assert "Copyright" not in everything and "About" not in everything
        assert result["total_parts"] == 3 and result["truncated"] is False
        urls = [link["url"] for link in result["links"]]
        assert web.url + "/templates" in urls
        assert "https://help.harbourline.example/refunds" in urls
        assert web.url + "/about" not in urls        # the nav's links are boilerplate too
        # An honest User-Agent that names the platform.
        assert all("DecentAI" in agent for agent in web.user_agents)

    def test_a_long_page_is_read_in_parts_with_totals(self, agents, web):
        sections = "".join(
            f"<h2>Clause {n}</h2><p>{'The studio keeps records of every project. ' * 40}</p>"
            for n in range(1, 13))
        web.page("/terms", f"<html><head><title>Terms</title></head><body>{sections}</body></html>")
        ex, _ = make()
        first, status = invoke(agents, ex, "pages_read",
                               {"url": web.url + "/terms", "max_chars": 4000})
        assert status == "success", first
        assert first["total_parts"] == 12
        assert first["truncated"] is True
        assert sum(len(p["text"]) for p in first["parts"]) <= 4000
        following, status = invoke(agents, ex, "pages_read",
                                   {"url": web.url + "/terms", "from": first["next"],
                                    "max_chars": 4000})
        assert status == "success", following
        assert following["parts"][0]["number"] == first["next"]
        assert following["parts"][0]["heading"] == f"## Clause {first['next']}"
        beyond, status = invoke(agents, ex, "pages_read",
                                {"url": web.url + "/terms", "from": 40})
        assert status == "error" and "12 sections" in beyond["error"]

    def test_a_pdf_reads_page_by_page(self, agents, web):
        url = web.page("/report.pdf", text_pdf(REPORT_PAGES, title="Annual Report 2026"),
                       "application/pdf")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url})
        assert status == "success", result
        assert (result["kind"], result["unit"], result["total_parts"]) == ("pdf", "page", 3)
        assert result["title"] == "Annual Report 2026"
        assert [p["number"] for p in result["parts"]] == [1, 2, 3]
        assert "second office in Lisbon" in result["parts"][1]["text"]
        assert result["unreadable_pages"] == []

    def test_plain_text_and_json_pass_through(self, agents, web):
        ex, _ = make()
        text_url = web.page("/notes.txt", "Opening hours\nMonday to Friday, 9 to 5.", "text/plain")
        result, status = invoke(agents, ex, "pages_read", {"url": text_url})
        assert status == "success", result
        assert result["kind"] == "text"
        assert result["parts"][0]["text"] == "Opening hours\nMonday to Friday, 9 to 5."
        json_url = web.page("/status.json", '{"status": "operational"}', "application/json")
        result, status = invoke(agents, ex, "pages_read", {"url": json_url})
        assert status == "success" and result["kind"] == "json"
        assert '"operational"' in result["parts"][0]["text"]

    def test_an_image_is_named_not_read(self, agents, web):
        url = web.page("/logo.png", b"\x89PNG\r\n\x1a\n" + bytes(200), "image/png")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url})
        assert status == "error" and result["kind"] == "unsupported"
        assert "image/png" in result["error"]

    def test_an_error_status_is_reported(self, agents, web):
        url = web.fail("/down", 503)
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url})
        assert status == "error" and result["kind"] == "http"
        # An error's other keys travel in its words, as JSON.
        assert '"status": 503' in result["error"]


class TestFinding:
    def test_found_phrases_are_quoted_with_their_section_and_absent_ones_named(self, agents, web):
        url = web.page("/pricing", PRICING)
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_find", {
            "url": url, "phrases": ["costs USD 12 a month for", "refunded", "cancellation fee"]})
        assert status == "success", result
        found = {row["phrase"]: row for row in result["phrases"]}
        # A line break in the page does not hide a phrase.
        assert found["costs USD 12 a month for"]["found"] is True
        assert found["refunded"]["count"] == 1
        assert result["absent"] == ["cancellation fee"]
        assert "cancellation fee" in result["note"]
        refund = next(m for m in result["matches"] if m["phrase"] == "refunded")
        assert refund["heading"] == "## Refunds" and refund["number"] == 3
        assert "refunded within 30 days" in refund["quote"]

    def test_find_in_a_pdf_names_the_page(self, agents, web):
        url = web.page("/report.pdf", text_pdf(REPORT_PAGES), "application/pdf")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_find",
                                {"url": url, "phrase": "currency"})
        assert status == "success", result
        assert [m["number"] for m in result["matches"]] == [3]
        assert result["absent"] == []

    def test_no_phrase_is_refused(self, agents, web):
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_find", {"url": web.url + "/x"})
        assert status == "error" and result["kind"] == "invalid"


class TestSaving:
    def test_a_web_page_is_saved_as_markdown_with_its_source(self, agents, web):
        url = web.page("/pricing", PRICING)
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_save", {"url": url})
        assert status == "success", result
        # Named for its title, with characters unsafe in a filename replaced.
        assert result["filename"] == "Harbourline Studio _ Pricing.md"
        assert result["mime_type"] == "text/markdown"
        stored = ex.files[result["file_ref"]]["content"]
        text = stored.decode("utf-8")
        assert text.startswith("# Harbourline Studio — Pricing")
        assert f"Source: {url}" in text and f"Fetched: {result['fetched_at']}" in text
        assert "## Refunds" in text and "tracking" not in text
        assert result["size"] == len(stored)

    def test_a_pdf_is_saved_as_its_original_bytes(self, agents, web):
        raw = text_pdf(REPORT_PAGES)
        url = web.page("/files/annual-report.pdf", raw, "application/pdf")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_save", {"url": url})
        assert status == "success", result
        assert result["filename"] == "annual-report.pdf"
        assert ex.files[result["file_ref"]]["name"] == "annual-report.pdf"
        assert ex.files[result["file_ref"]]["content"] == raw

    def test_documents_reads_what_was_saved(self, agents, web):
        """The point of saving: another agent reads the chat's file by its ref."""
        page_url = web.page("/pricing", PRICING)
        pdf_url = web.page("/report.pdf", text_pdf(REPORT_PAGES), "application/pdf")
        ex, _ = make()
        page, _ = invoke(agents, ex, "pages_save", {"url": page_url})
        pdf, _ = invoke(agents, ex, "pages_save", {"url": pdf_url})

        text, status = run(ex.invoke(agents["documents"], "read_text",
                                     {"file_ref": page["file_ref"]}, chat_level=0))
        assert status == "success", text
        assert "Annual plans can be refunded within 30 days" in text["pages"][0]["text"]
        text, status = run(ex.invoke(agents["documents"], "read_text",
                                     {"file_ref": pdf["file_ref"]}, chat_level=0))
        assert status == "success", text
        assert [p["page"] for p in text["pages"]] == [1, 2, 3]
        assert "Lisbon" in text["pages"][1]["text"]

    def test_a_pdf_over_a_chat_files_most_is_refused_with_its_size_and_link(self, agents, web):
        """The server says the size, so the refusal comes before the bytes:
        the words are true (no "read it in parts" for a PDF) and give the link."""
        size = FILE_MOST + 1024
        url = web.page("/big.pdf", b"%PDF-1.4\n" + b"0" * (size - 9), "application/pdf")
        ex, _ = make()
        for function in ("pages_save", "pages_read"):
            result, status = invoke(agents, ex, function, {"url": url})
            assert status == "error", result
            assert result["kind"] == "too_large"
            assert f'"size": {size}' in result["error"] and f'"limit": {FILE_MOST}' in result["error"]
            assert f"{size:,} bytes" in result["error"] and f"{FILE_MOST:,} bytes" in result["error"]
            assert url in result["error"] and "in parts" not in result["error"]
        assert not ex.files

    def test_a_pdf_over_the_old_five_mb_is_read_and_saved(self, agents, web):
        raw = padded_pdf(REPORT_PAGES, 6 * 1024 * 1024)
        assert 6 * 1024 * 1024 < len(raw) < FILE_MOST
        url = web.page("/big-report.pdf", raw, "application/pdf")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url})
        assert status == "success", result
        assert result["total_parts"] == 3 and "Lisbon" in result["parts"][1]["text"]
        saved, status = invoke(agents, ex, "pages_save", {"url": url})
        assert status == "success", saved
        assert ex.files[saved["file_ref"]]["content"] == raw

    def test_saving_runs_at_the_lowest_trust(self, agents, web):
        url = web.page("/pricing", PRICING)
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_save", {"url": url}, chat_level=0)
        assert status == "success", result

    def test_a_bot_check_is_not_saved_as_the_page(self, agents, web):
        url = web.page("/wall", CHALLENGE)
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_save", {"url": url})
        assert status == "error" and result["kind"] == "blocked", result
        assert '"needs_browser": true' in result["error"] and not ex.files


class TestAddressSafety:
    @pytest.mark.parametrize("url, why", [
        ("http://10.0.0.1/admin", "private"),
        ("http://169.254.169.254/latest/meta-data/", "link-local"),
        ("http://[::ffff:192.168.1.10]/", "private"),
        ("http://0.0.0.0:8080/", "unspecified"),
    ])
    def test_internal_addresses_are_refused_even_with_the_test_variable(self, agents, web, url, why):
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url})
        assert status == "error", result
        assert result["kind"] == "forbidden" and why in result["error"]

    def test_loopback_is_refused_without_the_test_variable(self, agents, monkeypatch):
        monkeypatch.delenv(LOOPBACK_VARIABLE, raising=False)
        stub = WebStub().start()
        try:
            url = stub.page("/pricing", PRICING)
            ex, _ = make()
            result, status = invoke(agents, ex, "pages_read", {"url": url})
            assert status == "error", result
            assert result["kind"] == "forbidden" and "loopback" in result["error"]
            assert stub.hits == {}
        finally:
            stub.stop()

    def test_a_redirect_to_a_forbidden_address_is_refused(self, agents, web):
        url = web.redirect("/go", "http://169.254.169.254/latest/meta-data/iam")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url})
        assert status == "error", result
        assert result["kind"] == "forbidden" and "169.254.169.254" in result["error"]

    def test_redirects_stop_after_five(self, agents, web):
        for n in range(7):
            web.redirect(f"/hop{n}", f"/hop{n + 1}")
        web.page("/hop7", "<p>arrived</p>")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": web.url + "/hop0"})
        assert status == "error" and result["kind"] == "too_many_redirects"
        assert web.hits.get("/hop6") is None

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "ftp://files.harbourline.example/report.pdf",
        "http://admin:secret@www.harbourline.example/",
    ])
    def test_other_schemes_and_credentials_are_refused(self, agents, url):
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url})
        assert status == "error" and result["kind"] == "invalid_url", result

    def test_a_huge_body_is_read_only_up_to_the_cap(self, agents, web):
        url = web.page("/huge.txt", "line of text\n" * 700_000, "text/plain")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read",
                                {"url": url, "max_chars": 4000})
        assert status == "success", result
        assert result["body_truncated"] is True
        assert result["total_chars"] <= FILE_MOST and "Only the first" in result["note"]

    def test_the_connected_address_is_checked_too(self, web, monkeypatch):
        """A name that passed the check but connects somewhere internal
        (DNS rebinding) is refused at the socket, where there is no way
        out to leave the name to. Exercised in-process, with the name check
        switched off, since no test resolver can rebind a name."""
        from agents.web_reader.tools.fetch import PROXY_VARIABLE, FetchError, SafeFetcher

        url = web.page("/pricing", PRICING)
        monkeypatch.delenv(LOOPBACK_VARIABLE)
        monkeypatch.delenv(PROXY_VARIABLE, raising=False)
        fetcher = SafeFetcher("DecentAI-test")
        fetcher.check_url = lambda _url: None
        with pytest.raises(FetchError) as refused:
            fetcher.get(url)
        assert refused.value.kind == "forbidden"
        assert web.hits == {}


class TestBehindThePlatformsWayOut:
    """In the sandbox a program's one way out is a proxy. A fetch goes
    through it in a tunnel and leaves the name to it; what this agent
    refuses by itself, it still refuses. A host the way out refuses
    (403) is forbidden; one it could not reach is the network's, in the
    way out's own words."""

    @pytest.fixture
    def proxy(self, agents, web, monkeypatch):
        """The test's own way out, with a lookup table of its own;
        ``admitted`` runs the agent as if it declared these hosts and no
        others."""
        found = way_out()
        found.names = {"inside.harbourline.example": ["10.0.0.5"],
                       "nowhere.harbourline.example": []}
        found.admitted = lambda hosts: monkeypatch.setitem(
            agents, "web_reader", declaring(agents["web_reader"], hosts))
        return found

    def port(self, web):
        return web.url.rsplit(":", 1)[1]

    def test_a_page_is_read_through_it_by_name(self, agents, web, proxy):
        web.page("/pricing", PRICING)
        proxy.admitted("any")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {
            "url": f"http://www.harbourline.example:{self.port(web)}/pricing"})
        assert status == "success", result
        assert result["title"] == "Harbourline Studio — Pricing"
        assert web.hits == {"/pricing": 1}
        assert proxy.refusals == 0

    def test_it_is_the_way_out_and_not_a_way_beside_one(self, agents, web, proxy):
        """The same page, and a manifest that declares nothing: had the
        fetch gone around the way out, it would have arrived."""
        url = web.page("/pricing", PRICING)
        proxy.admitted([])
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url})
        assert status == "error", result
        assert result["kind"] == "forbidden"
        assert web.hits == {}

    def test_a_name_that_resolves_inside_is_refused_by_it(self, agents, web, proxy):
        proxy.admitted("any")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {
            "url": "http://inside.harbourline.example/admin"})
        assert status == "error", result
        assert result["kind"] == "network"
        assert "not a public address" in result["error"]
        assert proxy.refusals == 0

    def test_an_encrypted_page_refused_says_why_too(self, agents, web, proxy):
        proxy.admitted("any")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {
            "url": "https://inside.harbourline.example/admin"})
        assert status == "error", result
        assert result["kind"] == "network"
        assert "not a public address" in result["error"]

    def test_an_address_written_as_one_is_refused_before_it_is_asked(
            self, agents, web, proxy):
        proxy.admitted("any")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {
            "url": "http://169.254.169.254/latest/meta-data/"})
        assert status == "error", result
        assert result["kind"] == "forbidden" and "link-local" in result["error"]
        assert proxy.refusals == 0

    def test_a_redirect_inside_is_refused_at_the_hop(self, agents, web, proxy):
        url = web.redirect("/go", "http://inside.harbourline.example/admin")
        proxy.admitted("any")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url})
        assert status == "error", result
        assert "not a public address" in result["error"]
        assert web.hits == {"/go": 1}

    def test_a_name_that_does_not_resolve_is_the_networks(self, agents, web, proxy):
        proxy.admitted("any")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {
            "url": "http://nowhere.harbourline.example/"})
        assert status == "error", result
        assert result["kind"] == "network"
        assert "does not resolve" in result["error"]


# -- what a plain fetch cannot see, charsets, long PDFs, and the loop -------------------------------------------------

CHALLENGE = """<!DOCTYPE html><html><head><title>Just a moment...</title>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script></head>
<body><div class="main-wrapper"><h1>Checking your browser before accessing harbourline.example</h1>
<noscript>Enable JavaScript and cookies to continue</noscript></div></body></html>"""

SHELL = ("<!doctype html><html><head><title>Harbourline App</title>"
         "<script defer src=\"/static/js/main.4f2a.js\"></script></head><body>"
         "<noscript>You need to enable JavaScript to run this app.</noscript>"
         "<div id=\"root\"></div></body></html>")

SIGN_IN = """<html><head><title>Sign in - Harbourline</title></head><body>
<form action="/session" method="post"><h1>Sign in</h1>
<label>Email <input type="email" name="email"></label>
<label>Password <input type="password" name="password"></label>
<button>Sign in</button></form><script src="/login.js"></script></body></html>"""

ARABIC = ("<html><head><title>أسعار استوديو هاربورلاين</title></head><body>"
          "<h1>الأسعار</h1><p>تبلغ تكلفة الخطة الأساسية 12 دولارًا شهريًا.</p>"
          "<h2>الاسترداد</h2><p>يمكن استرداد الخطط السنوية خلال ثلاثين يومًا.</p></body></html>")


def padded_pdf(pages, at_least: int) -> bytes:
    """text_pdf, grown past ``at_least`` bytes by a comment after its
    header, every offset in its cross-reference table moved to match."""
    raw = text_pdf(pages)
    pad = b"%" + b"x" * at_least + b"\n"
    head, rest = raw.split(b"\n", 1)
    body, xref = rest.split(b"xref\n", 1)
    xref = re.sub(rb"(\d{10}) 00000 n", lambda m: b"%010d 00000 n" % (int(m.group(1)) + len(pad)), xref)
    xref = re.sub(rb"startxref\n(\d+)", lambda m: b"startxref\n%d" % (int(m.group(1)) + len(pad)), xref)
    return head + b"\n" + pad + body + b"xref\n" + xref


class SlowRoutes(dict):
    """The stub's routes, some answered only after a wait."""

    def __init__(self, routes, slow):
        super().__init__(routes)
        self.slow = slow

    def get(self, key, default=None):
        if key in self.slow:
            time.sleep(self.slow[key])
        return super().get(key, default)


class TestWhatOnlyABrowserSees:
    def test_a_javascript_shell_says_it_needs_a_browser(self, agents, web):
        url = web.page("/app", SHELL)
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url})
        assert status == "success", result
        assert result["needs_browser"] is True and result["browser_reason"] == "javascript"
        assert "JavaScript" in result["note"]

    def test_a_large_page_of_scripts_with_few_words_needs_a_browser_too(self, agents, web):
        scripts = "".join(f"<script>window.chunk{n} = '{'x' * 900}';</script>" for n in range(60))
        url = web.page("/spa", f"<html><head>{scripts}</head><body><h1>Harbourline</h1>"
                               f"<p>Loading your dashboard, one moment please.</p></body></html>")
        ex, _ = make()
        result, _ = invoke(agents, ex, "pages_read", {"url": url})
        assert result["needs_browser"] is True and result["browser_reason"] == "javascript"

    def test_a_small_page_without_scripts_is_simply_small(self, agents, web):
        url = web.page("/hello", "<html><body><p>Opening hours: 9 to 5.</p></body></html>")
        ex, _ = make()
        result, _ = invoke(agents, ex, "pages_read", {"url": url})
        assert result["needs_browser"] is False and "browser_reason" not in result

    def test_a_full_page_with_a_noscript_note_is_read_as_it_is(self, agents, web):
        url = web.page("/pricing", PRICING.replace(
            "<main>", "<main><noscript>Enable JavaScript for live chat.</noscript>"))
        ex, _ = make()
        result, _ = invoke(agents, ex, "pages_read", {"url": url})
        assert result["needs_browser"] is False

    @pytest.mark.parametrize("status", [200, 403, 503])
    def test_a_bot_check_is_named_whatever_its_status(self, agents, web, status):
        web.routes["/wall"] = (status, {"Content-Type": "text/html; charset=utf-8"}, CHALLENGE.encode())
        ex, _ = make()
        result, outcome = invoke(agents, ex, "pages_read", {"url": web.url + "/wall"})
        if status == 200:
            assert outcome == "success" and result["needs_browser"] is True
            assert result["browser_reason"] == "blocked" and "Cloudflare" in result["note"]
        else:
            assert outcome == "error" and result["kind"] == "blocked", result
            assert '"needs_browser": true' in result["error"] and "Cloudflare" in result["error"]

    def test_a_sign_in_is_named(self, agents, web):
        web.redirect("/account", "/login")
        web.page("/login", SIGN_IN)
        web.routes["/private"] = (401, {"Content-Type": "text/plain"}, b"Unauthorized")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": web.url + "/account"})
        assert status == "success" and result["needs_browser"] is True
        assert result["browser_reason"] == "sign_in"
        result, status = invoke(agents, ex, "pages_read", {"url": web.url + "/private"})
        assert status == "error" and result["kind"] == "sign_in"
        assert '"needs_browser": true' in result["error"]

    def test_the_instructions_hand_such_a_page_to_the_browser(self, agents):
        words = agents["web_reader"].manifest["agent"]["instructions"]
        assert "browse_run" in words and "needs_browser" in words and "sign" in words


class TestCharsets:
    def test_arabic_in_windows_1256_named_by_the_server(self, agents, web):
        url = web.page("/ar", ARABIC.encode("cp1256"), "text/html; charset=windows-1256")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url})
        assert status == "success", result
        assert result["title"] == "أسعار استوديو هاربورلاين" and result["charset"] == "cp1256"
        assert "تبلغ تكلفة الخطة الأساسية" in result["parts"][0]["text"]

    def test_arabic_in_windows_1256_named_only_by_its_meta(self, agents, web):
        page = ARABIC.replace("<head>", '<head><meta http-equiv="Content-Type" '
                                        'content="text/html; charset=windows-1256">')
        url = web.page("/ar-meta", page.encode("cp1256"), "text/html")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_find", {"url": url, "phrase": "الخطط السنوية"})
        assert status == "success", result
        assert result["absent"] == [] and result["matches"][0]["heading"] == "## الاسترداد"

    def test_a_bom_wins_over_a_wrong_header(self, agents, web):
        url = web.page("/bom", b"\xef\xbb\xbf" + ARABIC.encode("utf-8"), "text/html; charset=iso-8859-1")
        ex, _ = make()
        result, _ = invoke(agents, ex, "pages_read", {"url": url})
        assert result["title"] == "أسعار استوديو هاربورلاين"

    def test_undeclared_non_utf8_is_guessed_never_failed(self, agents, web):
        words = ("Le café de la place est ouvert du lundi au vendredi. Nos crèmes brûlées sont préparées "
                 "chaque matin; la façade a été rénovée l'été dernier. Déjà venu ? Señor, bienvenue.")
        url = web.page("/latin", f"<html><body><p>{words}</p></body></html>".encode("cp1252"), "text/html")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url})
        assert status == "success", result
        assert result["parts"][0]["text"] == words and result["charset"] == "cp1252"

    def test_undeclared_arabic_in_windows_1256_is_guessed(self, agents, web):
        words = ("يمكن استرداد الخطط السنوية خلال ثلاثين يومًا من تاريخ الشراء، "
                 "ويشمل ذلك جميع الخطط المدفوعة مسبقًا.")
        url = web.page("/ar-plain", f"<html><body><p>{words}</p></body></html>".encode("cp1256"), "text/html")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url})
        assert status == "success", result
        assert result["parts"][0]["text"] == words and result["charset"] == "cp1256"

    def test_an_arabic_pdf_name_survives_saving(self, agents, web):
        url = web.page("/files/%D8%AA%D9%82%D8%B1%D9%8A%D8%B1.pdf", text_pdf(REPORT_PAGES), "application/pdf")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_save", {"url": url})
        assert status == "success", result
        assert result["filename"] == "تقرير.pdf"


class TestLongPdfs:
    def test_pages_past_four_hundred_are_named_and_read_next(self, agents, web):
        pages = [[f"Page {n} of the Harbourline archive."] for n in range(1, 451)]
        pages[429] = ["Page 430 holds the Lisbon lease."]
        url = web.page("/archive.pdf", text_pdf(pages), "application/pdf")
        ex, _ = make()
        first, status = invoke(agents, ex, "pages_read", {"url": url, "max_chars": 60000})
        assert status == "success", first
        assert first["total_parts"] == 450 and first["parts"][-1]["number"] == 400
        assert first["truncated"] is True and first["next"] == 401
        assert "pages 401–450 were not" in first["note"]
        rest, _ = invoke(agents, ex, "pages_read", {"url": url, "from": 401, "max_chars": 60000})
        assert [p["number"] for p in rest["parts"]] == list(range(401, 451))
        assert rest["truncated"] is False

        found, _ = invoke(agents, ex, "pages_find", {"url": url, "phrase": "Lisbon lease"})
        assert found["absent"] == ["Lisbon lease"] and found["next"] == 401
        assert found["searched"] == {"from": 1, "to": 400, "of": 450}
        assert "do not say the document lacks it" in found["note"]
        found, _ = invoke(agents, ex, "pages_find", {"url": url, "phrase": "Lisbon lease", "from": 401})
        assert [m["number"] for m in found["matches"]] == [430]

    def test_a_clipped_pdf_page_says_so(self, agents, web):
        line = "The studio keeps a record of every commission and its terms."
        url = web.page("/long.pdf", text_pdf([[line] * 2000, ["Short page."]]), "application/pdf")
        ex, _ = make()
        result, status = invoke(agents, ex, "pages_read", {"url": url, "max_chars": 60000})
        assert status == "success", result
        first = result["parts"][0]
        assert first["clipped"] is True and first["chars"] > 100_000
        assert len(first["text"]) == 60000
        assert result["next"] == 2 and "clipped" in result["note"]


class TestOffTheLoop:
    def test_a_slow_site_does_not_hold_up_another_call(self, agents, web):
        web.page("/slow", PRICING)
        web.page("/fast", PRICING)
        web.routes = SlowRoutes(web.routes, {"/slow": 4})
        ex, _ = make()
        invoke(agents, ex, "pages_read", {"url": web.url + "/fast"})       # the program started
        finished = {}

        async def both():
            started = time.monotonic()

            async def one(path):
                result = await ex.invoke(agents["web_reader"], "pages_read", {"url": web.url + path})
                finished[path] = time.monotonic() - started
                return result

            return await asyncio.gather(one("/slow"), one("/fast"))

        (slow, _), (fast, _) = run(both())
        assert slow["title"] == fast["title"]
        assert finished["/fast"] < 2 < finished["/slow"]
