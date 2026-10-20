"""Regression tests for defects found in the pre-release adversarial audit (second round). Offline."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "check-references" / "scripts" / "citecheck.py"
sys.path.insert(0, str(SCRIPT.parent))
sys.path.insert(0, str(ROOT / "tests"))

import citecheck as cc  # noqa: E402
from test_citecheck import FakeVerifier, LODOCO, hit, ref  # noqa: E402


class Identifiers(unittest.TestCase):
    def test_sici_doi_kept_whole(self):
        txt = "Newcombe RG. Two-sided confidence intervals for the single proportion: comparison of seven methods. Stat Med. 1998;17(8):857-872. doi:10.1002/(SICI)1097-0258(19980430)17:8<857::AID-SIM777>3.0.CO;2-E"
        r = cc.parse_free_citation(txt, "r")
        self.assertEqual(r.doi, "10.1002/(sici)1097-0258(19980430)17:8<857::aid-sim777>3.0.co;2-e")
        self.assertEqual(r.first_author, "Newcombe")

    def test_doi_from_publisher_url_drops_query_and_fragment(self):
        self.assertEqual(cc.clean_doi("10.1056/NEJMoa2021372?query=recirc_curatedRelated_article"), "10.1056/nejmoa2021372")
        self.assertEqual(cc.clean_doi("10.1056/NEJMoa2021372#article_references"), "10.1056/nejmoa2021372")
        self.assertEqual(cc.clean_doi("https://doi.org/10.1136/bmj.n71/full"), "10.1136/bmj.n71")

    def test_pmid_from_pubmed_url(self):
        r = cc.parse_free_citation("Tardif JC, Kouz S. Efficacy and Safety of Low-Dose Colchicine after Myocardial Infarction. N Engl J Med. 2019;381(26):2497-2505. https://pubmed.ncbi.nlm.nih.gov/31733140/", "r")
        self.assertEqual(r.pmid, "31733140")
        self.assertFalse(r.is_web_resource)

    def test_second_doi_recorded(self):
        r = cc.parse_free_citation("Nidorf SM, et al. Colchicine in Patients with Chronic Coronary Disease. N Engl J Med. 2020;383(19):1838-1847. doi:10.1056/NEJMoa2021372. Supplementary Appendix doi:10.1056/NEJMoa2099999.", "r")
        self.assertEqual(r.doi, "10.1056/nejmoa2021372")
        self.assertEqual(r.extra_dois, ["10.1056/nejmoa2099999"])


class Authors(unittest.TestCase):
    def test_non_ascii_and_suffixed_surnames_parse(self):
        r = cc.parse_free_citation("Özdemir A, Fiolet ATL, Mosterd A, et al. Colchicine in Patients with Chronic Coronary Disease. N Engl J Med. 2020;383(19):1838-1847.", "r")
        self.assertEqual(r.first_author, "Özdemir")
        r = cc.parse_free_citation("Smith AB Jr, Williamson JD, Whelton PK, et al. A Randomized Trial of Intensive versus Standard Blood-Pressure Control. N Engl J Med. 2015;373(22):2103-2116.", "r")
        self.assertEqual(r.first_author, "Smith")
        r = cc.parse_free_citation("Kannel WB, Dawber TR, Kagan A, Revotskie N, Stokes J 3rd. Factors of risk in the development of coronary heart disease--six year follow-up experience. The Framingham Study. Ann Intern Med. 1961;55:33-50.", "r")
        self.assertEqual(r.first_author, "Kannel")
        self.assertEqual(r.year, "1961")

    def test_group_author(self):
        r = cc.parse_free_citation("SPRINT Research Group. A Randomized Trial of Intensive versus Standard Blood-Pressure Control. N Engl J Med. 2015;373(22):2103-2116.", "r")
        self.assertEqual(r.first_author, "SPRINT Research Group")
        self.assertTrue(r.title.startswith("A Randomized Trial"))

    def test_et_al_only_list_is_stripped(self):
        r = cc.parse_free_citation("et al. Colchicine in Patients with Chronic Coronary Disease. N Engl J Med. 2020;383(19):1838-1847.", "r")
        self.assertEqual(r.title, "Colchicine in Patients with Chronic Coronary Disease")
        self.assertEqual(r.volume, "383")

    def test_all_caps_reference_is_linear_time(self):
        txt = "EFFICACY AND SAFETY OF LOW DOSE COLCHICINE AFTER MYOCARDIAL INFARCTION IN PATIENTS WITH TYPE TWO DIABETES MELLITUS AND CHRONIC KIDNEY DISEASE AND MORE WORDS HERE. N ENGL J MED 2019;381:2497-505."
        t0 = time.time()
        cc.parse_free_citation(txt, "r")
        cc.looks_like_citation(txt)
        self.assertLess(time.time() - t0, 0.5)
        t0 = time.time()
        cc.extract_claims("(" + "Smith et al. " * 30 + ")")
        self.assertLess(time.time() - t0, 0.5)


class Labels(unittest.TestCase):
    def test_retracted_label_requires_publisher_form(self):
        self.assertTrue(cc.RETRACTED_LABEL_RE.match("RETRACTED: Hydroxychloroquine or chloroquine ..."))
        self.assertTrue(cc.RETRACTED_LABEL_RE.match("WITHDRAWN: Some paper"))
        self.assertFalse(cc.RETRACTED_LABEL_RE.match("Retracted Publications in the Biomedical Literature from Open Access Journals"))
        self.assertFalse(cc.RETRACTED_LABEL_RE.match("Retracted science and the retraction index"))

    def test_notice_prefix(self):
        self.assertTrue(cc.NOTICE_PREFIX_RE.match("Correction to: Carotid Atherosclerosis in Patients with Atrial Fibrillation"))
        self.assertTrue(cc.NOTICE_PREFIX_RE.match("Erratum: something"))
        self.assertFalse(cc.NOTICE_PREFIX_RE.match("Correcting the record on colchicine"))
        # a notice title does not get the subtitle-containment boost against the article title
        self.assertLess(cc.title_similarity("Carotid Atherosclerosis in Patients with Atrial Fibrillation", "Correction to: Carotid Atherosclerosis in Patients with Atrial Fibrillation"), cc.TITLE_MATCH)

    def test_sister_journals_do_not_match(self):
        self.assertFalse(cc.journals_match("BMJ", ["BMJ Open"]))
        self.assertFalse(cc.journals_match("Lancet", ["The Lancet Digital Health"]))
        self.assertFalse(cc.journals_match("Nature", ["Nature Medicine"]))
        self.assertTrue(cc.journals_match("Eur Heart J", ["European Heart Journal"]))
        self.assertTrue(cc.journals_match("JAMA Cardiol", ["JAMA cardiology"]))


class VerifyRules(unittest.TestCase):
    def test_doiorg_only_is_unverifiable(self):
        hits = [cc.Hit(source="crossref", query="doi:x", ok=True, found=False), cc.Hit(source="datacite", query="doi:x", ok=True, found=False),
                cc.Hit(source="doi.org", query="doi:x", ok=True, found=True, doi="10.5281/zenodo.1"), cc.Hit(source="openalex", query="doi:x", ok=False, error="HTTP 503")]
        res = FakeVerifier(doi_hits=hits).verify(ref(title="Turmeric Infusion in Cardiogenic Shock", first_author="Smith", year="2023", doi="10.5281/zenodo.1"))
        self.assertEqual(res.status, "UNVERIFIABLE")
        self.assertIn("DOI_RESOLVES_NO_METADATA", res.flags)

    def test_registry_404_beats_secondary_index(self):
        hits = [cc.Hit(source="crossref", query="doi:x", ok=True, found=False), cc.Hit(source="doi.org", query="doi:x", ok=True, found=False),
                hit("openalex", **dict(LODOCO, doi="10.1016/s0140-6736"))]
        res = FakeVerifier(doi_hits=hits).verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020", doi="10.1016/s0140-6736"))
        self.assertEqual(res.status, "NOT_FOUND")

    def test_notice_doi_cited_is_mismatch(self):
        h = hit("crossref", **dict(LODOCO, title="Correction to: Colchicine in Patients with Chronic Coronary Disease"))
        h.notice_type, h.notice_for = "correction", "10.1056/nejmoa2021372"
        res = FakeVerifier(doi_hits=[h]).verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020", doi="10.1056/nejmoa2021372.corr"))
        self.assertEqual(res.status, "MISMATCH")
        self.assertIn("NOTICE_DOI_CITED", res.flags)

    def test_weak_retraction_hint_alone_is_not_retracted(self):
        h = hit("crossref", **LODOCO)
        h.weak_retraction = True
        res = FakeVerifier(doi_hits=[h]).verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020", doi="10.1056/nejmoa2021372"))
        self.assertNotEqual(res.status, "RETRACTED")
        self.assertIn("RETRACTION_HINT", res.flags)

    def test_web_resource_is_unverifiable_not_matched(self):
        r = cc.parse_free_citation("World Health Organization. Cardiovascular diseases (CVDs) fact sheet. https://www.who.int/news-room/fact-sheets/detail/cardiovascular-diseases-(cvds)", "r")
        self.assertTrue(r.is_web_resource)
        res = FakeVerifier(title_hits=[cc.Hit(source="openalex", query="t", ok=True, candidates=[{"source": "openalex", "title": "Bulletin of the World Health Organization", "first_author": "van der Sande", "year": "2001", "journal": "", "doi": "10.1/x", "pmid": ""}])]).verify(r)
        self.assertEqual(res.status, "UNVERIFIABLE")
        self.assertIn("WEB_RESOURCE", res.flags)

    def test_title_only_needs_author_or_year(self):
        cand = {"source": "openalex", "doi": "10.1/a", "pmid": "", "title": LODOCO["title"], "first_author": "Nidorf", "year": "2020", "journal": "NEJM"}
        th = cc.Hit(source="openalex", query="t", ok=True, candidates=[cand])
        th2 = cc.Hit(source="europepmc", query="t", ok=True, candidates=[])
        v = FakeVerifier(title_hits=[th, th2], sources=("openalex", "europepmc"))
        bare = cc.Ref(ref_id="r", title=LODOCO["title"], raw=LODOCO["title"])
        res = v.verify(bare)
        self.assertEqual(res.status, "UNVERIFIABLE")
        self.assertIn("TITLE_ONLY_UNCONFIRMED", res.flags)

    def test_letter_sharing_title_rejected_when_volume_and_pages_differ(self):
        trial = {"source": "openalex", "doi": "10.1056/nejmoa2021372", "pmid": "", "title": LODOCO["title"], "first_author": "", "year": "2020", "journal": "New England Journal of Medicine", "volume": "383", "pages": "1838-1847", "type": "article"}
        letter = {"source": "crossref", "doi": "10.1056/nejmc2034992", "pmid": "", "title": LODOCO["title"], "first_author": "Fischer", "year": "2021", "journal": "New England Journal of Medicine", "volume": "384", "pages": "776-779", "type": "journal-article"}
        v = FakeVerifier(title_hits=[cc.Hit(source="openalex", query="t", ok=True, candidates=[letter, trial]), cc.Hit(source="europepmc", query="t", ok=True)], sources=("openalex", "europepmc"))
        res = v.verify(ref(title=LODOCO["title"], year="2020", volume="383", pages="1838-1847", journal="N Engl J Med"))
        self.assertEqual(res.status, "VERIFIED")
        self.assertEqual(res.resolved["doi"], "10.1056/nejmoa2021372")

    def test_author_not_parsed_is_a_warning_not_verified(self):
        r = cc.parse_free_citation("SMITH AB, JONES CD, WILLIAMS EF. Colchicine in Patients with Chronic Coronary Disease. N Engl J Med. 2020;383(19):1838-1847. doi:10.1056/NEJMoa2021372", "r")
        self.assertEqual(r.first_author, "")
        self.assertTrue(r.author_parse_failed)
        res = FakeVerifier(doi_hits=[hit("crossref", **LODOCO)]).verify(r)
        self.assertEqual(res.status, "VERIFIED_WITH_WARNINGS")
        self.assertIn("AUTHOR_NOT_CHECKED", res.flags)

    def test_title_search_with_failed_sources_is_unverifiable(self):
        th = [cc.Hit(source="pubmed", query="t", ok=False, error="efetch failed"), cc.Hit(source="openalex", query="t", ok=False, error="HTTP 503"), cc.Hit(source="europepmc", query="t", ok=True), cc.Hit(source="crossref", query="t", ok=False, error="HTTP 503")]
        res = FakeVerifier(title_hits=th, sources=("pubmed", "openalex", "europepmc", "crossref")).verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020"))
        self.assertEqual(res.status, "UNVERIFIABLE")

    def test_confidence_counts_databases(self):
        res = FakeVerifier(doi_hits=[hit("crossref", **LODOCO)]).verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020", doi="10.1056/nejmoa2021372"))
        self.assertEqual(res.confidence, "medium")
        res = FakeVerifier(doi_hits=[hit("crossref", **LODOCO), hit("openalex", **LODOCO)]).verify(ref(title=LODOCO["title"], first_author="Ebrahimi", year="2020", doi="10.1056/nejmoa2021372"))
        self.assertEqual((res.status, res.confidence), ("MISMATCH", "high"))


class ReportingAndCli(unittest.TestCase):
    def test_submission_safe_requires_everything_checked(self):
        rows = [cc.Result(ref_id="a", status="VERIFIED", confidence="high"), cc.Result(ref_id="b", status="UNVERIFIABLE", confidence="low")]
        audit = cc.build_audit(rows, "ms.md", {"sources": ["crossref"]})
        self.assertFalse(audit["submission_safe"])
        self.assertEqual(audit["unchecked_ref_ids"], ["b"])
        self.assertIn("1 of 2 references were checked", audit["method_statement"])
        self.assertIn("NOT CHECKED", cc.audit_markdown(audit))
        off = cc.build_audit(rows, "ms.md", {"sources": ["crossref"], "offline": True})
        self.assertIn("NO database was queried", off["method_statement"])

    def test_cli_offline_exit_4_and_files(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "qc"
            p = subprocess.run([sys.executable, str(SCRIPT), "verify", str(ROOT / "tests" / "fixtures" / "vancouver_refs.txt"), "--offline", "--out-dir", str(out), "--quiet"], capture_output=True, text=True)
            self.assertEqual(p.returncode, 4, p.stderr)
            audit = json.loads((out / "reference_audit.json").read_text(encoding="utf-8"))
            self.assertFalse(audit["submission_safe"])
            self.assertTrue((out / "reference_audit.md").exists())
            self.assertNotIn(str(Path.home()), json.dumps(audit))

    def test_cli_usage_errors_exit_2(self):
        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4 not really")
            p = subprocess.run([sys.executable, str(SCRIPT), "verify", str(pdf), "--out-dir", td], capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)
            self.assertIn("PDF", p.stderr)
            p = subprocess.run([sys.executable, str(SCRIPT), "verify", str(ROOT / "tests" / "fixtures" / "refs.bib"), "--offline", "--sources", "pubmd", "--out-dir", td], capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)

    def test_cache_never_stores_email_or_key(self):
        with tempfile.TemporaryDirectory() as td:
            http = cc.Http(email="alice@example.org", cache_path=Path(td) / "c.json")
            fake_resp = mock.MagicMock()
            fake_resp.status = 200
            fake_resp.geturl.return_value = "https://eutils.ncbi.nlm.nih.gov/x?db=pubmed&email=alice%40example.org&api_key=SECRET"
            fake_resp.read.return_value = b"{}"
            fake_resp.__enter__.return_value = fake_resp
            with mock.patch("urllib.request.OpenerDirector.open", return_value=fake_resp):
                http.get("https://eutils.ncbi.nlm.nih.gov/x?db=pubmed&email=alice%40example.org&api_key=SECRET")
            http.save_cache()
            text = (Path(td) / "c.json").read_text(encoding="utf-8")
            self.assertNotIn("alice", text)
            self.assertNotIn("SECRET", text)
            self.assertIn("REDACTED", text)

    def test_user_agent_email_only_for_crossref(self):
        http = cc.Http(email="alice@example.org")
        self.assertIn("alice@example.org", http.user_agent_for("api.crossref.org"))
        self.assertNotIn("alice", http.user_agent_for("doi.org"))
        self.assertNotIn("alice", http.user_agent_for("www.ebi.ac.uk"))

    def test_future_dated_cache_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            cp = Path(td) / "c.json"
            cp.write_text(json.dumps({"GET https://api.crossref.org/works/x": {"status": 200, "body": "{}", "final_url": "", "ts": time.time() + 10 ** 9}}), encoding="utf-8")
            http = cc.Http(cache_path=cp)
            self.assertEqual(http.cache, {})


class Claims(unittest.TestCase):
    def test_narrative_and_latex_citations(self):
        self.assertEqual([c["cites"] for c in cc.extract_claims("Nidorf et al. (2020) reported a 31% reduction.")], [["Nidorf et al., 2020"]])
        self.assertEqual([c["cites"] for c in cc.extract_claims("Colchicine helped \\cite{nidorf2020,tardif2019} in trials.")], [["nidorf2020", "tardif2019"]])
        self.assertEqual([c["cites"] for c in cc.extract_claims("Guidelines differ (e.g., van der Meer et al., 2019; Nidorf et al., 2020a, 2020b).")], [["van der Meer et al., 2019", "Nidorf et al., 2020a, 2020b"]])

    def test_numbers_exclude_citation_tokens(self):
        c = cc.extract_claims("Events fell (HR 0.77, 95% CI 0.61-0.96) [2].")[0]
        self.assertEqual(c["cites"], ["2"])
        self.assertIn("0.77", c["numbers"])
        self.assertNotIn("2", c["numbers"])


if __name__ == "__main__":
    unittest.main()
