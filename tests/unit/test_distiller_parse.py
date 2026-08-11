"""Locating the payload in a real model's response (§5.4).

The response is not guaranteed to be pure JSON. A reasoning model emits a
preamble, wraps its answer in a code fence, or narrates around it. Taking the
first object that happens to parse is a guess, and the expensive failure is not
the loud one -- it is silently accepting a leading object that happens to carry
a ``products`` key, whose products then validate and publish.
"""

import json

import pytest

from memory_net.distiller.pipeline import (
    AmbiguousProductsPayload,
    parse_json_response,
)

PAYLOAD = {"products": [{"topic": "retry"}]}


def test_a_pure_json_response_parses() -> None:
    assert parse_json_response(json.dumps(PAYLOAD)) == PAYLOAD


def test_a_preamble_object_is_skipped_in_favour_of_the_payload() -> None:
    """Taking the first object would raise "must include products[]" against
    something that was never the answer."""
    text = f'{{"thinking": "let me consider"}}\n{json.dumps(PAYLOAD)}'

    assert parse_json_response(text) == PAYLOAD


def test_a_leading_object_that_merely_has_products_no_longer_wins_silently() -> None:
    """The worst case of "take the first": a plausible-looking wrong payload
    that validates and publishes."""
    decoy = {"products": [{"topic": "draft, ignore this"}]}
    text = f"{json.dumps(decoy)}\n{json.dumps(PAYLOAD)}"

    with pytest.raises(AmbiguousProductsPayload, match="2 objects"):
        parse_json_response(text)


def test_a_fenced_payload_is_found() -> None:
    text = f"Here is the result:\n```json\n{json.dumps(PAYLOAD)}\n```\nDone."

    assert parse_json_response(text) == PAYLOAD


def test_a_trailing_note_after_the_payload_is_ignored() -> None:
    assert parse_json_response(f"{json.dumps(PAYLOAD)}\nHope that helps!") == PAYLOAD


def test_no_object_with_products_reports_the_candidate_keys() -> None:
    """Naming what was found is the difference between a two-minute diagnosis
    and re-running a paid call to look at the response."""
    text = '{"thinking": "hmm"}\n{"answer": "none"}'

    with pytest.raises(ValueError, match="no object with products") as raised:
        parse_json_response(text)

    assert "answer" in str(raised.value)
    assert "thinking" in str(raised.value)


def test_the_failure_message_never_echoes_a_value() -> None:
    """It lands in parse_error_details, which lands in published metrics."""
    text = '{"thinking": "the api key is sk-live-topsecret"}'

    with pytest.raises(ValueError) as raised:
        parse_json_response(text)

    assert "sk-live-topsecret" not in str(raised.value)


def test_a_response_with_no_json_at_all_still_raises_a_decode_error() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_json_response("I could not complete this request.")


def test_a_products_key_that_is_not_a_list_is_refused() -> None:
    with pytest.raises(ValueError, match="products"):
        parse_json_response('{"products": "retry"}')


def test_a_long_response_around_the_payload_is_still_handled() -> None:
    """Bounded scanning, not a heuristic on the first N characters."""
    noise = "thinking out loud. " * 2000
    text = f"{noise}{json.dumps(PAYLOAD)}{noise}"

    assert parse_json_response(text) == PAYLOAD


def test_a_nested_products_key_does_not_count_as_a_second_payload() -> None:
    """Only sibling objects are candidates. Scanning every brace descends into
    the payload's own nesting, and a product carrying a nested ``products`` key
    then looks like a rival payload -- turning a perfectly good response into
    an ambiguity error. The preamble matters: without it the whole text parses
    in one go and this scanner is never reached."""
    payload = {"products": [{"topic": "retry", "meta": {"products": 1}}]}
    text = f"Thinking out loud.\n{json.dumps(payload)}"

    assert parse_json_response(text) == payload


def test_a_payload_wrapped_in_an_array_is_still_found() -> None:
    """Some models answer with a one-element list. The object inside it is a
    sibling as far as the scan is concerned, which is what makes it findable."""
    text = f"Result:\n[{json.dumps(PAYLOAD)}]"

    assert parse_json_response(text) == PAYLOAD


def test_a_products_key_inside_a_string_is_not_an_object() -> None:
    text = f'{{"thinking": "I will emit {{\\"products\\": []}} next"}}\n{json.dumps(PAYLOAD)}'

    assert parse_json_response(text) == PAYLOAD


def test_a_truncated_trailing_object_does_not_hide_the_payload() -> None:
    """A response cut off at max_tokens still has a usable payload before the
    cut, and refusing it would discard work already paid for."""
    text = f'{json.dumps(PAYLOAD)}\n{{"note": "cut off here'

    assert parse_json_response(text) == PAYLOAD
