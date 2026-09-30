"""Jewelry campaign pull — the deterministic parts (the model's reading is reviewed by
humans via the Evidence column, not asserted here)."""

import re

from aflalo_cs import jewelry_pull as jp


def test_catalog_leads_match_casual_mentions_but_not_clothing():
    pat = jp._term_pattern(["Hex Ring in Gold", "Bicep Bangle in Silver", "Tennis Drop Earrings in Mixed Gold"])
    assert pat.search("do you still have the hex ring?")
    assert pat.search("price for the bicep bangle please")
    assert pat.search("Tennis Drop Earrings")
    assert not pat.search("the Freja Pant in Silk in a 4")


def test_a_domestic_price_ask_is_filed_as_domestic_not_group_one():
    thread = {"thread_id": "t1", "subject": "Romani Cuff", "messages": [{"received_at": "2026-08-20T10:00:00+00:00"}]}
    verdict = {"is_jewelry_inquiry": True, "customer_name": "Tamara", "customer_email": "t@example.com",
               "country": "United States", "international": "no", "pieces": ["Romani Cuff in Silver"],
               "asked_pricing": True, "received_pricing": False, "interest_after_pricing": False,
               "group": "pricing_only", "evidence": "IN NY", "summary": "asked price"}
    row = jp.to_row(thread, verdict)
    assert row["Group"] == "Domestic" and row["International"] == "No"


def test_our_own_address_never_becomes_the_campaign_email():
    thread = {"thread_id": "t2", "subject": "Fwd", "messages": [{"received_at": "2026-08-20T10:00:00+00:00"}]}
    verdict = {"is_jewelry_inquiry": True, "customer_name": "Ava", "customer_email": "ava@aflalonyc.com",
               "country": "unknown", "international": "unknown", "pieces": [], "asked_pricing": False,
               "received_pricing": False, "interest_after_pricing": False, "group": "unclear",
               "evidence": "", "summary": ""}
    assert jp.to_row(thread, verdict)["Email"] == ""


def test_report_separates_the_two_groups_and_names_countries():
    rows = [
        {"Customer": "A", "Email": "a@x.de", "Country": "Germany", "International": "Yes", "Group": "Pricing only", "Pieces": "Hex Ring", "Subject": ""},
        {"Customer": "B", "Email": "b@x.ca", "Country": "Canada", "International": "Yes", "Group": "Real interest after pricing", "Pieces": "Tile Necklace", "Subject": ""},
        {"Customer": "C", "Email": "c@x.com", "Country": "unknown", "International": "Unknown", "Group": "Pricing only", "Pieces": "", "Subject": "Cuff"},
        {"Customer": "D", "Email": "d@x.com", "Country": "United States", "International": "No", "Group": "Domestic", "Pieces": "", "Subject": ""},
    ]
    text = jp.report(rows)
    assert "GROUP 1 — international, asked pricing only (1)" in text
    assert "GROUP 2 — international, real interest after pricing (1)" in text
    assert "Country unknown from the thread (check before adding) (1)" in text
    assert re.search(r"Countries seen.*Canada, Germany", text)
