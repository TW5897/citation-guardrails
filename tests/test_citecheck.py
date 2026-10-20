"""Offline tests for citecheck.py. No network: lookups are stubbed.

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "check-references" / "scripts"))

import citecheck as cc  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def hit(source="crossref", **kw) -> cc.Hit:
    h = cc.Hit(source=source, query=kw.pop("query", "doi:x"), found=True, ok=True)
    for k, v in kw.items():
        setattr(h, k, v)
    if h.authors and not h.first_author:
        h.first_author = h.authors[0]
    return h


LODOCO = dict(title="Colchicine in Patients with Chronic Coronary Disease", authors=["Nidorf", "Fiolet", "Mosterd"],
              year="2020", journal="The New England journal of medicine", journal_abbrev="N Engl J Med",
              volume="383", issue="19", pages="1838-1847", doi="10.1056/nejmoa2021372", pmid="32865380")


class FakeVerifier(cc.Verifier):
    """Verifier whose network lookups return canned hits."""

    def __init__(self, doi_hits=None, pmid_hits=None, title_hits=None, sources=("crossref", "openalex", "europepmc")):
        super().__init__(http=None, sources=sources, resolve_doi_org=False, quiet=True)
        self.doi_hits, self.pmid_hits, self.title_hits = doi_hits or [], pmid_hits or [], title_hits or []

    def lookup_doi(self, doi):
        return self.doi_hits

    def lookup_pmid(self, pmid):
        return self.pmid_hits

    def lookup_title(self, ref):
        return self.title_hits


def ref(**kw) -> cc.Ref:
    r = cc.Ref(ref_id=kw.pop("ref_id", "r1"), **kw)
    if r.first_author and not r.authors:
        r.authors = [r.first_author]
    return r


class TextUtils(unittest.TestCase):
    def test_title_similarity(self):
        self.assertGreaterEqual(cc.title_similarity("Colchicine in Patients with Chronic Coronary Disease", "Colchicine in Patients with Chronic Coronary Disease."), 0.99)
        self.assertGreaterEqual(cc.title_similarity("colchicine in patients with chronic coronary disease", "RETRACTED: Colchicine in Patients with Chronic Coronary Disease"), 0.99)
        self.assertLess(cc.title_similarity("Efficacy and Safety of Low-Dose Colchicine after Myocardial Infarction", "Colchicine in Patients with Chronic Coronary Disease"), cc.TITLE_WARN)
        # dropped subtitle still matches
        self.assertGreaterEqual(cc.title_similarity("The PRISMA 2020 statement", "The PRISMA 2020 statement: an updated guideline for reporting systematic reviews"), cc.TITLE_MATCH)

    def test_surnames(self):
        self.assertTrue(cc.surnames_match("van der Meer", "Meer"))
        self.assertTrue(cc.surnames_match("Abd-Alrazaq", "Abd Alrazaq"))
        self.assertTrue(cc.surnames_match("Müller", "Muller"))
        self.assertFalse(cc.surnames_match("Nidorf", "Ebrahimi"))
        self.assertTrue(cc.surnames_match("", "Nidorf"), "unknown cited surname must not flag")
        self.assertEqual(cc.surname_from_display("Stefan Mark Nidorf"), "Nidorf")
        self.assertEqual(cc.surname_from_display("Nidorf SM"), "Nidorf")
        self.assertEqual(cc.surname_from_display("van der Meer P"), "van der Meer")
        self.assertEqual(cc.surname_from_display("Nidorf, Stefan M."), "Nidorf")

    def test_journals(self):
        self.assertTrue(cc.journals_match("N Engl J Med", ["The New England journal of medicine"]))
        self.assertTrue(cc.journals_match("Lancet", ["The Lancet"]))
        self.assertTrue(cc.journals_match("J Am Coll Cardiol", ["Journal of the American College of Cardiology"]))
        self.assertFalse(cc.journals_match("Lancet", ["JAMA"]))

    def test_doi_cleaning(self):
        self.assertEqual(cc.clean_doi("https://doi.org/10.1016/S0140-6736(20)31180-6)."), "10.1016/s0140-6736(20)31180-6")
        self.assertEqual(cc.clean_doi("doi: 10.1056/NEJMoa2021372,"), "10.1056/nejmoa2021372")

    def test_redact_url(self):
        u = "https://eutils.ncbi.nlm.nih.gov/x?db=pubmed&email=me%40x.org&api_key=SECRET&id=1"
        r = cc.redact_url(u)
        self.assertNotIn("me%40x.org", r)
        self.assertNotIn("SECRET", r)
        self.assertIn("id=1", r)

    def test_vancouver_name(self):
        self.assertEqual(cc._vancouver_name("Nidorf, Stefan M"), "Nidorf SM")
        self.assertEqual(cc._vancouver_name("Nidorf, SM"), "Nidorf SM")
        self.assertEqual(cc._vancouver_name("Tardif, Jean-Claude"), "Tardif JC")
        self.assertEqual(cc._vancouver_name("LoDoCo2 Trial Investigators"), "LoDoCo2 Trial Investigators")


class Parsers(unittest.TestCase):
    def test_vancouver_fixture(self):
        refs = cc.parse_input(FIX / "vancouver_refs.txt")
        self.assertEqual(len(refs), 8)
        by = {r.ref_id: r for r in refs}
        self.assertEqual(by["ref_1"].doi, "10.1056/nejmoa2021372")
        self.assertEqual(by["ref_1"].title, "Colchicine in Patients with Chronic Coronary Disease")
        self.assertEqual(by["ref_1"].journal, "N Engl J Med")
        self.assertEqual((by["ref_1"].volume, by["ref_1"].pages), ("383", "1838-1847"))
        self.assertEqual(by["ref_2"].pmid, "39034931")
        self.assertEqual(by["ref_2"].first_author, "Warsi")
        self.assertEqual(by["ref_7"].first_author, "van der Meer")
        self.assertTrue(by["ref_7"].title.startswith("ACC/AHA Versus ESC"))
        self.assertEqual((by["ref_8"].doi, by["ref_8"].pmid), ("10.1056/nejmoa1409077", "25176015"))
        self.assertEqual(by["ref_5"].doi, "10.1016/s0140-6736(20)31180-6")

    def test_apa_fixture(self):
        refs = cc.parse_input(FIX / "apa_refs.txt")
        self.assertEqual(len(refs), 3)
        self.assertEqual(refs[0].first_author, "Nidorf")
        self.assertEqual(refs[0].year, "2020")
        self.assertEqual(refs[0].doi, "10.1056/nejmoa2021372")
        self.assertGreaterEqual(cc.title_similarity(refs[0].title, "Colchicine in Patients with Chronic Coronary Disease"), 0.99)
        self.assertEqual(refs[1].first_author, "Tardif")
        self.assertEqual(refs[2].doi, "10.9999/fake.2022.001")

    def test_bibtex_fixture(self):
        refs = cc.parse_input(FIX / "refs.bib")
        self.assertEqual([r.ref_id for r in refs], ["Nidorf_2020_LoDoCo2", "Warsi_2024_EFE", "Frankenstein_2020", "Fake_2023_Turmeric"])
        self.assertEqual(refs[0].pages, "1838-1847")
        self.assertEqual(refs[1].pmid, "39034931")
        self.assertEqual(refs[2].first_author, "Ebrahimi")

    def test_ris_fixture(self):
        refs = cc.parse_input(FIX / "refs.ris")
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0].doi, "10.1056/nejmoa2021372")
        self.assertEqual(refs[1].first_author, "Mehra")

    def test_manuscript_numeric(self):
        refs = cc.parse_input(FIX / "manuscript_numeric.md")
        self.assertEqual(len(refs), 6)
        prisma = refs[4]
        self.assertEqual(prisma.year, "2021", "year inside the title must not be taken as the publication year")
        self.assertEqual(prisma.pages, "n71")
        self.assertEqual(prisma.doi, "10.1136/bmj.n71")

    def test_manuscript_without_reference_list_yields_nothing(self):
        refs = cc.parse_input(FIX / "manuscript_pandoc.md")
        self.assertEqual(refs, [])

    def test_refs_from_ids(self):
        r = cc.refs_from_ids(["The PRISMA 2020 statement: an updated guideline for reporting systematic reviews"])[0]
        self.assertEqual(r.year, "")
        self.assertEqual(r.first_author, "")
        self.assertTrue(r.title.startswith("The PRISMA 2020"))
        self.assertEqual(cc.refs_from_ids(["10.1056/NEJMoa2021372"])[0].doi, "10.1056/nejmoa2021372")
        self.assertEqual(cc.refs_from_ids(["PMID: 32865380"])[0].pmid, "32865380")
        v = cc.refs_from_ids(["Nidorf SM, et al. Colchicine in Patients with Chronic Coronary Disease. N Engl J Med. 2020;383:1838-47."])[0]
        self.assertEqual((v.first_author, v.year, v.journal), ("Nidorf", "2020", "N Engl J Med"))


class Claims(unittest.TestCase):
    def test_numeric_claims(self):
        text = (FIX / "manuscript_numeric.md").read_text(encoding="utf-8")
        claims = cc.extract_claims(text)
        self.assertEqual(len(claims), 6)
        self.assertEqual(claims[0]["cites"], ["1"])
        self.assertIn("31%", claims[0]["numbers"])
        self.assertEqual(claims[0]["claim_type"], "numeric")
        prisma = [c for c in claims if "PRISMA" in c["sentence"]][0]
        self.assertEqual(prisma["cites"], ["5", "6"])
        self.assertFalse(any("Fig. 1 shows" in c["sentence"] for c in claims))
        self.assertFalse(any("own data" in c["sentence"] for c in claims))

    def test_pandoc_claims(self):
        text = (FIX / "manuscript_pandoc.md").read_text(encoding="utf-8")
        claims = cc.extract_claims(text)
        self.assertEqual(len(claims), 3)
        self.assertEqual(sorted(claims[1]["cites"]), ["Frankenstein_2020", "Warsi_2024_EFE"])

    def test_range_expansion(self):
        self.assertEqual(cc.expand_numeric("1-3, 7"), ["1", "2", "3", "7"])


class VerifyLogic(unittest.TestCase):
    def test_verified_two_sources(self):
        v = FakeVerifier(doi_hits=[hit("crossref", **LODOCO), hit("openalex", **LODOCO)])
        res = v.verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020", journal="N Engl J Med", doi="10.1056/nejmoa2021372"))
        self.assertEqual(res.status, "VERIFIED")
        self.assertEqual(res.confidence, "high")
        self.assertEqual(res.sources_confirming, ["crossref", "openalex"])

    def test_frankenstein_author(self):
        v = FakeVerifier(doi_hits=[hit("crossref", **LODOCO)])
        res = v.verify(ref(title=LODOCO["title"], first_author="Ebrahimi", year="2020", doi="10.1056/nejmoa2021372"))
        self.assertEqual(res.status, "MISMATCH")
        self.assertTrue(any("first author mismatch" in i for i in res.issues))

    def test_frankenstein_title(self):
        v = FakeVerifier(doi_hits=[hit("crossref", **LODOCO)])
        res = v.verify(ref(title="Efficacy and Safety of Low-Dose Colchicine after Myocardial Infarction", first_author="Nidorf", year="2020", doi="10.1056/nejmoa2021372"))
        self.assertEqual(res.status, "MISMATCH")

    def test_not_found_doi(self):
        nf = cc.Hit(source="crossref", query="doi:x", ok=True, found=False)
        nf2 = cc.Hit(source="openalex", query="doi:x", ok=True, found=False)
        v = FakeVerifier(doi_hits=[nf, nf2], title_hits=[cc.Hit(source="openalex", query="t", ok=True), cc.Hit(source="crossref", query="t", ok=True)])
        res = v.verify(ref(title="Some Fabricated Paper Title Here", first_author="Smith", year="2020", doi="10.1056/nejmoa9999999"))
        self.assertEqual(res.status, "NOT_FOUND")

    def test_unverifiable_when_all_sources_fail(self):
        bad = cc.Hit(source="crossref", query="doi:x", ok=False, found=False, error="HTTP 503")
        v = FakeVerifier(doi_hits=[bad])
        res = v.verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020", doi="10.1056/nejmoa2021372"))
        self.assertEqual(res.status, "UNVERIFIABLE")

    def test_retracted(self):
        h = hit("crossref", **LODOCO)
        h.retracted = True
        h.retraction_notes = ["Retraction (CrossRef update-to)"]
        v = FakeVerifier(doi_hits=[h])
        res = v.verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020", doi="10.1056/nejmoa2021372"))
        self.assertEqual(res.status, "RETRACTED")
        self.assertIn("RETRACTED", res.flags)

    def test_year_warning(self):
        v = FakeVerifier(doi_hits=[hit("crossref", **LODOCO)])
        res = v.verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2021", doi="10.1056/nejmoa2021372"))
        self.assertEqual(res.status, "VERIFIED_WITH_WARNINGS")

    def test_identifier_conflict(self):
        other = dict(title="Risk Factors of Acute Ischemic Stroke and Mortality Among Adults With Endocardial Fibroelastosis", authors=["Warsi"], year="2024", journal="The neurologist", pmid="39034931", doi="10.1097/nrl.0000000000000576")
        v = FakeVerifier(doi_hits=[hit("crossref", **LODOCO)], pmid_hits=[hit("europepmc", query="pmid:39034931", **other)])
        res = v.verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020", doi="10.1056/nejmoa2021372", pmid="39034931"))
        self.assertEqual(res.status, "MISMATCH")
        self.assertIn("IDENTIFIER_CONFLICT", res.flags)

    def test_preprint_flag(self):
        h = hit("crossref", **dict(LODOCO, doi="10.1101/2020.01.01.20000001", work_type="posted-content"))
        h.preprint = True
        h.preprint_server = "medRxiv"
        v = FakeVerifier(doi_hits=[h])
        res = v.verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020", doi="10.1101/2020.01.01.20000001"))
        self.assertEqual(res.status, "VERIFIED")
        self.assertIn("PREPRINT", res.flags)

    def test_title_only_accepted_and_ambiguous(self):
        cand = {"source": "openalex", "doi": "", "pmid": "", "title": LODOCO["title"], "first_author": "Nidorf", "year": "2020", "journal": "New England Journal of Medicine"}
        cand2 = dict(cand, journal="Journal of Imaginary Reprints", doi="10.1/other")
        th = cc.Hit(source="openalex", query="t", ok=True, candidates=[cand, cand2])
        th2 = cc.Hit(source="europepmc", query="t", ok=True, candidates=[])
        v = FakeVerifier(title_hits=[th, th2], sources=("openalex", "europepmc"))
        res = v.verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020"))
        self.assertEqual(res.status, "VERIFIED")
        self.assertEqual(res.found_by, "title")
        self.assertIn("TITLE_MATCHED", res.flags)
        self.assertIn("MULTIPLE_MATCHES", res.flags)
        # with the journal cited, the ambiguity resolves
        res2 = v.verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020", journal="N Engl J Med"))
        self.assertNotIn("MULTIPLE_MATCHES", res2.flags)

    def test_title_exists_but_author_differs(self):
        cand = {"source": "openalex", "doi": "", "pmid": "", "title": LODOCO["title"], "first_author": "Nidorf", "year": "2020", "journal": "NEJM"}
        th = cc.Hit(source="openalex", query="t", ok=True, candidates=[cand])
        th2 = cc.Hit(source="europepmc", query="t", ok=True, candidates=[])
        v = FakeVerifier(title_hits=[th, th2], sources=("openalex", "europepmc"))
        res = v.verify(ref(title=LODOCO["title"], first_author="Ebrahimi", year="2015"))
        self.assertEqual(res.status, "NOT_FOUND")
        self.assertIn("TITLE_EXISTS_AUTHOR_YEAR_DIFFER", res.flags)

    def test_title_only_network_failure_is_unverifiable(self):
        th = cc.Hit(source="openalex", query="t", ok=False, error="HTTP 503")
        th2 = cc.Hit(source="europepmc", query="t", ok=False, error="HTTP 503")
        v = FakeVerifier(title_hits=[th, th2], sources=("openalex", "europepmc"))
        res = v.verify(ref(title="Some Real Looking Title Of A Paper", first_author="Smith", year="2020"))
        self.assertEqual(res.status, "UNVERIFIABLE")


class Reporting(unittest.TestCase):
    def test_audit_and_markdown(self):
        v = FakeVerifier(doi_hits=[hit("crossref", **LODOCO)])
        ok = v.verify(ref(ref_id="ref_1", title=LODOCO["title"], first_author="Nidorf", year="2020", doi="10.1056/nejmoa2021372"))
        nf = cc.Verifier.__new__(cc.Verifier)
        bad = cc.Result(ref_id="ref_2", status="NOT_FOUND", issues=["DOI does not exist"], cited={"raw": "Fake ref", "first_author": "Smith", "year": "2023"})
        audit = cc.build_audit([ok, bad], "ms.md", {"sources": ["crossref"]})
        self.assertFalse(audit["submission_safe"])
        self.assertEqual(audit["blocking_ref_ids"], ["ref_2"])
        md = cc.audit_markdown(audit)
        self.assertIn("NOT_FOUND", md)
        self.assertIn("Method statement", md)
        json.dumps(audit)  # serialisable

    def test_bibtex_entry_has_verification_stamp(self):
        rec = dict(LODOCO, authors_full=["Nidorf, Stefan M", "Fiolet, Aernoud T L"], sources=["crossref", "pubmed"])
        b = cc.bibtex_entry(rec)
        self.assertIn("verified  = {true}", b)
        self.assertIn("verified_by = {crossref+pubmed}", b)
        self.assertIn("Nidorf, Stefan M and Fiolet, Aernoud T L", b)

    def test_vancouver_string(self):
        rec = dict(LODOCO, authors_full=["Nidorf, Stefan M", "Fiolet, Aernoud T L"])
        s = cc.vancouver(rec)
        self.assertTrue(s.startswith("Nidorf SM, Fiolet ATL."))
        self.assertIn("2020;383(19):1838-1847", s)

    def test_jats_to_text(self):
        xml = "<article><front><article-meta><title-group><article-title>T</article-title></title-group></article-meta></front><body><sec><title>Methods</title><p>We did <italic>x</italic>.</p></sec></body><back><ref-list><ref><mixed-citation>ignored</mixed-citation></ref></ref-list></back></article>"
        t = cc.jats_to_text(xml)
        self.assertIn("Methods", t)
        self.assertIn("We did x.", t)
        self.assertNotIn("ignored", t)


if __name__ == "__main__":
    unittest.main()
