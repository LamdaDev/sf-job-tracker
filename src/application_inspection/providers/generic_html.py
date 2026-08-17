"""Semantic, dependency-free extraction from public static application HTML."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from ..models import ApplicationProvider, ApplicationQuestion, ApplicationScanResult, ScanStatus
from ..normalizer import normalize_label, normalize_question
from ..security import is_safe_public_http_url


_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_CONTROL_TAGS = {"input", "textarea", "select"}
_IGNORED_INPUT_TYPES = {"button", "hidden", "image", "reset", "submit"}
_PLACEHOLDER_OPTIONS = {"", "select", "select...", "select an option", "choose", "choose...", "--"}


class PublicFetchError(RuntimeError):
    """A public page could not be retrieved without any special access."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None]
    parent: "_Node | None" = None
    children: list["_Node | str"] = field(default_factory=list)

    def attr(self, name: str) -> str | None:
        return self.attrs.get(name.casefold())

    def has_attr(self, name: str) -> bool:
        return name.casefold() in self.attrs


class _TreeBuilder(HTMLParser):
    """Small DOM tree sufficient for labels, fieldsets, and form controls."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag.casefold(), {key.casefold(): value for key, value in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        if node.tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == lowered:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


@dataclass(frozen=True)
class PageSignals:
    """Non-invasive signals that make a form unavailable or incomplete."""

    captcha: bool = False
    login_required: bool = False
    closed: bool = False
    multi_step: bool = False


@dataclass(frozen=True)
class StaticExtraction:
    """Question definitions plus page-level safety/completeness evidence."""

    questions: tuple[ApplicationQuestion, ...]
    signals: PageSignals
    has_form: bool


@dataclass(frozen=True)
class FetchedPage:
    """A non-authenticated public HTTP response used by generic inspection."""

    text: str
    status: int
    resolved_url: str


def fetch_public_html(url: str, *, timeout_seconds: int = 25) -> FetchedPage:
    """Fetch public HTML with a bounded timeout and no credentials or cookies."""

    if not is_safe_public_http_url(url):
        raise PublicFetchError("Application URL is not a safe public HTTP(S) URL.")
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "sf-job-tracker-application-inspector/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 - public URL selected by a job feed
            body = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return FetchedPage(
                text=body.decode(charset, errors="replace"),
                status=response.getcode(),
                resolved_url=response.geturl(),
            )
    except HTTPError as error:
        raise PublicFetchError(f"Public page returned HTTP {error.code}", status=error.code) from error
    except (URLError, TimeoutError, OSError, UnicodeError) as error:
        raise PublicFetchError(f"Could not fetch public page: {type(error).__name__}") from error


def _walk(node: _Node) -> Iterable[_Node]:
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _walk(child)


def _text(node: _Node, *, omit_controls: bool = False) -> str:
    fragments: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            fragments.append(child)
        elif not (omit_controls and child.tag in _CONTROL_TAGS):
            fragments.append(_text(child, omit_controls=omit_controls))
    return normalize_label(" ".join(fragments))


def _has_ancestor(node: _Node, tag: str) -> bool:
    cursor = node.parent
    while cursor is not None:
        if cursor.tag == tag:
            return True
        cursor = cursor.parent
    return False


def _is_hidden(node: _Node) -> bool:
    cursor: _Node | None = node
    while cursor is not None:
        if cursor.has_attr("hidden") or (cursor.attr("aria-hidden") or "").casefold() == "true":
            return True
        style = (cursor.attr("style") or "").replace(" ", "").casefold()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        cursor = cursor.parent
    return False


def _required(node: _Node) -> bool | None:
    aria_required = (node.attr("aria-required") or "").casefold()
    if aria_required in {"true", "1"} or node.has_attr("required"):
        return True
    if aria_required in {"false", "0"}:
        return False
    return None


def _nearest(node: _Node, tag: str) -> _Node | None:
    cursor = node.parent
    while cursor is not None:
        if cursor.tag == tag:
            return cursor
        cursor = cursor.parent
    return None


def _fieldset_legend(node: _Node) -> str | None:
    fieldset = _nearest(node, "fieldset")
    if fieldset is None:
        return None
    for child in fieldset.children:
        if isinstance(child, _Node) and child.tag == "legend":
            label = _text(child)
            if label:
                return label
    return None


def _human_name(value: str | None) -> str:
    if not value:
        return ""
    candidate = value.replace("_", " ").replace("-", " ").strip()
    if candidate.casefold() in {"field", "input", "form", "value", "question"}:
        return ""
    return candidate[:1].upper() + candidate[1:]


def _label_for(
    node: _Node,
    *,
    labels_by_for: Mapping[str, str],
    nodes_by_id: Mapping[str, _Node],
) -> str:
    identifier = node.attr("id")
    if identifier and labels_by_for.get(identifier):
        return labels_by_for[identifier]
    wrapping_label = _nearest(node, "label")
    if wrapping_label is not None:
        label = _text(wrapping_label, omit_controls=True)
        if label:
            return label
    aria_labelledby = node.attr("aria-labelledby")
    if aria_labelledby:
        label = normalize_label(
            " ".join(_text(nodes_by_id[item]) for item in aria_labelledby.split() if item in nodes_by_id)
        )
        if label:
            return label
    if node.attr("aria-label"):
        return normalize_label(node.attr("aria-label") or "")
    legend = _fieldset_legend(node)
    if legend:
        return legend
    if node.attr("placeholder"):
        return normalize_label(node.attr("placeholder") or "")
    return _human_name(node.attr("name"))


def _option_text(option: _Node) -> str:
    return normalize_label(_text(option) or option.attr("label") or "")


def _select_options(node: _Node) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for child in _walk(node):
        if child.tag != "option" or child.has_attr("disabled"):
            continue
        value = _option_text(child)
        if value.casefold() in _PLACEHOLDER_OPTIONS:
            continue
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            output.append(value)
    return tuple(output)


def _page_signals(html: str) -> PageSignals:
    text = " ".join(html.casefold().split())
    captcha = any(
        signal in text
        for signal in (
            "captcha",
            "recaptcha",
            "hcaptcha",
            "cloudflare challenge",
            "verify you are human",
            "bot verification",
            "security check",
        )
    )
    closed = any(
        signal in text
        for signal in (
            "job no longer available",
            "position has been filled",
            "application closed",
            "this job is no longer available",
            "job is no longer available",
        )
    )
    login_required = any(
        signal in text
        for signal in (
            "sign in",
            "log in",
            "create account",
            "create an account",
            "verification code",
        )
    )
    multi_step = (
        "step 1" in text
        or "step 2" in text
        or "multi-step" in text
        or "multistep" in text
        or "of 2" in text and "step" in text
    )
    return PageSignals(captcha=captcha, login_required=login_required, closed=closed, multi_step=multi_step)


def extract_questions_from_html(html: str) -> StaticExtraction:
    """Extract semantic controls without executing scripts or altering a form."""

    parser = _TreeBuilder()
    parser.feed(html)
    parser.close()
    nodes = list(_walk(parser.root))
    nodes_by_id = {
        identifier: node
        for node in nodes
        if (identifier := node.attr("id"))
    }
    labels_by_for: dict[str, str] = {}
    for node in nodes:
        if node.tag != "label" or not (target := node.attr("for")):
            continue
        label = _text(node, omit_controls=True)
        if label:
            labels_by_for[target] = normalize_label(f"{labels_by_for.get(target, '')} {label}")

    has_form = any(node.tag == "form" or (node.attr("role") or "").casefold() == "form" for node in nodes)
    controls = [
        node
        for node in nodes
        if node.tag in _CONTROL_TAGS
        and not _is_hidden(node)
        and not (node.tag == "input" and (node.attr("type") or "text").casefold() in _IGNORED_INPUT_TYPES)
    ]
    # A page with a real form can contain unrelated search inputs in headers.
    if has_form:
        controls = [node for node in controls if _has_ancestor(node, "form") or _nearest(node, "form") is not None]

    control_positions = {id(node): position for position, node in enumerate(controls, start=1)}
    groups: dict[tuple[str, str, str], list[_Node]] = {}
    standalone: list[_Node] = []
    for node in controls:
        input_type = (node.attr("type") or "text").casefold() if node.tag == "input" else node.tag
        name = node.attr("name")
        if input_type in {"radio", "checkbox"} and name:
            form_key = str(id(_nearest(node, "form") or parser.root))
            groups.setdefault((form_key, input_type, name), []).append(node)
        else:
            standalone.append(node)

    extracted: list[ApplicationQuestion] = []
    for node in standalone:
        input_type = (node.attr("type") or "text").casefold() if node.tag == "input" else node.tag
        label = _label_for(node, labels_by_for=labels_by_for, nodes_by_id=nodes_by_id)
        options = _select_options(node) if node.tag == "select" else ()
        question = normalize_question(
            label=label,
            field_type=input_type,
            required=_required(node),
            options=options,
            source_section=_fieldset_legend(node),
            ordinal=control_positions[id(node)],
            field_name=node.attr("name"),
            multiple=node.tag == "select" and node.has_attr("multiple"),
        )
        if question is not None:
            extracted.append(question)

    for (_, input_type, name), members in groups.items():
        first = min(members, key=lambda item: control_positions[id(item)])
        section = _fieldset_legend(first)
        group_label = section or _label_for(first, labels_by_for=labels_by_for, nodes_by_id=nodes_by_id)
        option_labels = [
            _label_for(member, labels_by_for=labels_by_for, nodes_by_id=nodes_by_id)
            for member in members
        ]
        # A radio's own option label is not the question; prefer its fieldset
        # legend, then an explicit group aria label, and finally its name.
        if not section and len(members) > 1:
            group_label = _human_name(name)
        if not group_label:
            group_label = _human_name(name)
        member_requirements = tuple(_required(member) for member in members)
        question = normalize_question(
            label=group_label,
            field_type=input_type,
            required=(
                True
                if any(value is True for value in member_requirements)
                else False
                if any(value is False for value in member_requirements)
                else None
            ),
            options=option_labels,
            source_section=section,
            ordinal=control_positions[id(first)],
            field_name=name,
            multiple=input_type == "checkbox" and len(members) > 1,
        )
        if question is not None:
            extracted.append(question)

    # Browser-rendered DOMs occasionally duplicate controls for accessibility.
    # Keep the first public representation while preserving form order.
    deduplicated: list[ApplicationQuestion] = []
    seen: set[tuple[str, str, str | None]] = set()
    for question in sorted(extracted, key=lambda item: (item.ordinal, item.label.casefold())):
        identity = (question.label.casefold(), question.field_type, question.source_section)
        if identity not in seen:
            seen.add(identity)
            deduplicated.append(question)
    return StaticExtraction(tuple(deduplicated), _page_signals(html), has_form)


def find_public_apply_link(html: str, base_url: str) -> str | None:
    """Return one obvious, safe public application link without clicking it.

    This supports providers such as Lever where a public job-description URL
    can expose the form through a separate ``/apply`` route.  We only follow
    an explicit anchor whose visible text describes an application; arbitrary
    buttons and JavaScript handlers are intentionally ignored.
    """

    parser = _TreeBuilder()
    parser.feed(html)
    parser.close()
    try:
        base_host = (urlsplit(base_url).hostname or "").casefold()
    except ValueError:
        return None
    for node in _walk(parser.root):
        if node.tag != "a" or _is_hidden(node):
            continue
        href = node.attr("href")
        text = _text(node).casefold()
        if not href or not any(phrase in text for phrase in ("apply", "application")):
            continue
        candidate = urljoin(base_url, href)
        if not is_safe_public_http_url(candidate):
            continue
        try:
            candidate_host = (urlsplit(candidate).hostname or "").casefold()
        except ValueError:
            continue
        # Same-host navigation is routine.  A direct link to a known public
        # ATS is also acceptable; do not follow arbitrary third-party links.
        if candidate_host == base_host or candidate_host.endswith(".lever.co") or candidate_host.endswith(".greenhouse.io") or candidate_host.endswith(".ashbyhq.com") or candidate_host.endswith(".myworkdayjobs.com"):
            return candidate
    return None


def inspect_static_html(
    *,
    canonical_job_id: str,
    application_url: str,
    html: str,
    provider: ApplicationProvider = ApplicationProvider.GENERIC,
    http_status: int | None = None,
    scanned_at: str | None = None,
) -> ApplicationScanResult:
    """Turn public static HTML into a conservative scan result.

    Static HTML cannot prove that subsequent dynamic or conditional steps do
    not exist, so successful extraction is intentionally ``partial``.
    """

    extraction = extract_questions_from_html(html)
    if extraction.signals.closed:
        status = ScanStatus.UNAVAILABLE
        reason = "Application appears to be closed."
    elif extraction.signals.captcha:
        status = ScanStatus.UNAVAILABLE
        reason = "Application page requires anti-bot verification."
    elif extraction.signals.login_required and extraction.questions:
        status = ScanStatus.PARTIAL
        reason = "Additional application questions require authentication."
    elif extraction.signals.login_required:
        status = ScanStatus.UNAVAILABLE
        reason = "Application page requires authentication."
    elif extraction.questions and extraction.signals.multi_step:
        status = ScanStatus.PARTIAL
        reason = "Only the visible first step was inspected; later steps may contain more questions."
    elif extraction.questions:
        status = ScanStatus.PARTIAL
        reason = "Only publicly visible static fields were inspected; dynamic or conditional fields may remain."
    else:
        status = ScanStatus.UNAVAILABLE
        reason = "No publicly accessible application form fields were detected."
    return ApplicationScanResult(
        canonical_job_id=canonical_job_id,
        provider=provider,
        application_url=application_url,
        status=status,
        questions=extraction.questions,
        completeness_reason=reason,
        scanned_at=scanned_at,
        http_status=http_status,
        metadata={"has_form": extraction.has_form, "inspection_method": "static_html"},
    )
