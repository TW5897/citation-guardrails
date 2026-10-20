"""Additional offline tests written during the pre-release audit (test-coverage lens).

Run from the repo root:  python -m unittest discover -s tests -v
Tests 1-6 FAIL against citecheck v0.1.0 and document real defects; test 7 passes (coverage only).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "check-references" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import citecheck as cc  # noqa: E402
from test_citecheck import FakeVerifier, LODOCO, hit, ref  # noqa: E402

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
VANC = [
    "Nidorf SM, Fiolet ATL, Mosterd A, et al. Colchicine in Patients with Chronic Coronary Disease. N Engl J Med. 2020;383(19):1838-1847. doi:10.1056/NEJMoa2021372",
    "Tardif JC, Kouz S, Waters DD, et al. Efficacy and Safety of Low-Dose Colchicine after Myocardial Infarction. N Engl J Med. 2019;381(26):2497-2505. doi:10.1056/NEJMoa1912388",
    "Smith J, Jones A. Intravenous Turmeric and 30-Day Mortality in Cardiogenic Shock: The TURMERIC-SHOCK Randomized Trial. Lancet. 2023;401(10380):1120-1130.",
]


class AuditAdditions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # 1 ---------------------------------------------------------------- parsers: silent merging
    def test_1_reference_lists_are_not_merged(self):
        with self.subTest("APA list, one reference per line, no blank lines"):
            p = self.tmp / "apa.txt"
            p.write_text(
                "References\n"
                "Nidorf, S. M., Fiolet, A. T. L., & Mosterd, A. (2020). Colchicine in patients with chronic coronary disease. New England Journal of Medicine, 383(19), 1838-1847. https://doi.org/10.1056/NEJMoa2021372\n"
                "Tardif, J.-C., Kouz, S., & Waters, D. D. (2019). Efficacy and safety of low-dose colchicine after myocardial infarction. New England Journal of Medicine, 381(26), 2497-2505.\n"
                "Smith, J., & Jones, A. (2023). Intravenous turmeric and 30-day mortality in cardiogenic shock. The Lancet, 401(10380), 1120-1130.\n",
                encoding="utf-8")
            self.assertEqual(len(cc.parse_input(p)), 3)
        with self.subTest("DOCX whose reference list uses Word automatic numbering"):
            def para(t, num=False):
                ppr = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>' if num else ""
                return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{t}</w:t></w:r></w:p>'
            body = para("Colchicine reduced events [1].") + para("References") + "".join(para(r, True) for r in VANC)
            p = self.tmp / "ms.docx"
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{body}</w:body></w:document>')
            self.assertEqual(len(cc.parse_input(p)), 3)
        with self.subTest("BibTeX: a one-line entry must not swallow the next entry"):
            p = self.tmp / "refs.bib"
            p.write_text(
                "@article{tardif, author={Tardif, Jean-Claude}, title={Efficacy and Safety of Low-Dose Colchicine after Myocardial Infarction}, journal={N Engl J Med}, year={2019}, doi={10.1056/NEJMoa1912388}}\n"
                "@article{nidorf,\n  author = {Nidorf, Stefan M.},\n  title = {Colchicine in Patients with Chronic Coronary Disease},\n  year = {2020},\n  doi = {10.1056/NEJMoa2021372}\n}\n",
                encoding="utf-8")
            by = {r.ref_id: r for r in cc.parse_input(p)}
            self.assertEqual(sorted(by), ["nidorf", "tardif"])
            self.assertEqual(by["tardif"].doi, "10.1056/nejmoa1912388")

    # 2 ---------------------------------------------------------------- parsers: field corruption
    def test_2_csv_bom_and_ris_record_id(self):
        with self.subTest("CSV saved by Excel as 'CSV UTF-8' starts with a BOM"):
            p = self.tmp / "refs.csv"
            p.write_bytes("\ufefftitle,authors,year,doi\nColchicine in Patients with Chronic Coronary Disease,Nidorf SM,2020,10.1056/NEJMoa2021372\n".encode("utf-8"))
            self.assertEqual(cc.parse_input(p)[0].title, "Colchicine in Patients with Chronic Coronary Disease")
        with self.subTest("RIS: a numeric record ID is not a PMID"):
            p = self.tmp / "refs.ris"
            p.write_text("TY  - JOUR\nID  - 10234\nAU  - Nidorf, Stefan M.\nTI  - Colchicine in Patients with Chronic Coronary Disease\nPY  - 2020\nDO  - 10.1056/NEJMoa2021372\nER  - \n", encoding="utf-8")
            self.assertEqual(cc.parse_input(p)[0].pmid, "")

    # 3 ---------------------------------------------------------------- parsers: BibTeX-generated \bibitem
    def test_3_bibtex_generated_bibitem(self):
        p = self.tmp / "paper.tex"
        p.write_text("\\begin{thebibliography}{1}\n\\bibitem{nidorf}\nStefan~M. Nidorf, Aernoud~T.~L. Fiolet, and Arend Mosterd.\n"
                     "\\newblock Colchicine in patients with chronic coronary disease.\n"
                     "\\newblock {\\em New England Journal of Medicine}, 383(19):1838--1847, 2020.\n\\end{thebibliography}\n", encoding="utf-8")
        r = cc.parse_input(p)[0]
        self.assertEqual(r.first_author, "Nidorf")
        self.assertGreaterEqual(cc.title_similarity(r.title, LODOCO["title"]), cc.TITLE_MATCH)

    # 4 ---------------------------------------------------------------- verify: nothing compared -> not VERIFIED
    def test_4_doi_org_only_or_single_source_is_not_trusted(self):
        with self.subTest("DOI resolves only at doi.org; metadata sources failed or lack it"):
            hits = [cc.Hit(source="crossref", query="doi:x", ok=False, error="HTTP 503"),
                    cc.Hit(source="doi.org", query="doi:x", ok=True, found=True, doi="10.1056/nejmoa2021372"),
                    cc.Hit(source="openalex", query="doi:x", ok=False, error="HTTP 429"),
                    cc.Hit(source="europepmc", query="doi:x", ok=True, found=False)]
            res = FakeVerifier(doi_hits=hits).verify(ref(title="Turmeric Infusion in Cardiogenic Shock", first_author="Smith", year="2023", doi="10.1056/nejmoa2021372"))
            self.assertNotEqual(res.status, "VERIFIED", "no cited field was compared with any record")
        with self.subTest("confidence 'high' needs two databases (docs/how-it-works.md)"):
            oa = hit("openalex", **dict(LODOCO, year="2025"))
            res = FakeVerifier(doi_hits=[cc.Hit(source="crossref", query="doi:x", ok=True, found=False), oa]).verify(
                ref(title=LODOCO["title"], first_author="Nidorf", year="2020", doi="10.1056/nejmoa2021372"))
            self.assertEqual(res.sources_confirming, ["openalex"])
            self.assertNotEqual(res.confidence, "high")

    # 5 ---------------------------------------------------------------- reporting: nothing checked -> not safe
    def test_5_unverifiable_audit_is_not_submission_safe(self):
        rows = [cc.Result(ref_id=f"ref_{i}", status="UNVERIFIABLE", confidence="low") for i in (1, 2)]
        audit = cc.build_audit(rows, "ms.md", {"sources": ["crossref", "pubmed"]})
        self.assertFalse(audit["submission_safe"], "every row unchecked, yet the report says 'Submission safe: yes'")
        self.assertNotIn("All 2 references were checked", audit["method_statement"])

    # 6 ---------------------------------------------------------------- claims: invented and missed cites
    def test_6_claim_extraction(self):
        def cites(s):
            return [c["cites"] for c in cc.extract_claims(s)]
        with self.subTest("decimals inside a bracket-cited sentence"):
            self.assertEqual(cites("Events fell (HR 0.77, 95% CI 0.61-0.96) [2]."), [["2"]])
        with self.subTest("decimals in an uncited sentence"):
            self.assertEqual(cites("Mean age was 64.5 years in the cohort."), [])
            self.assertEqual(cites("The difference was significant (p=0.03, n=120)."), [])
        with self.subTest("pandoc superscript attached to a word / full stop"):
            self.assertEqual(cites("Colchicine reduced events^1^ in this trial."), [["1"]])
            self.assertEqual(cites("Colchicine reduced events.^1^ Next sentence here."), [["1"]])
        with self.subTest("author-year narrative and particle citations"):
            self.assertEqual(len(cc.extract_claims("Nidorf et al. (2020) reported a 31% reduction.")), 1)
            self.assertEqual(len(cc.extract_claims("Heart failure guidelines differ (van der Meer et al., 2019).")), 1)

    # 7 ---------------------------------------------------------------- coverage only (passes today)
    def test_7_untested_paths_that_pass(self):
        with self.subTest("DOI-only citation enriched via PubMed picks up a PubMed-only retraction"):
            ph = hit("pubmed", **LODOCO)
            ph.retracted, ph.retraction_notes = True, ["PubMed RetractionIn: x"]
            v = FakeVerifier(doi_hits=[hit("openalex", **LODOCO)], sources=("crossref", "pubmed", "openalex"))
            with mock.patch.object(cc, "pubmed_by_pmid", return_value=ph) as m:
                res = v.verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020", doi="10.1056/nejmoa2021372"))
            m.assert_called_once()
            self.assertEqual(res.status, "RETRACTED")
            self.assertEqual(res.sources_confirming, ["openalex", "pubmed"])
        with self.subTest("a journal difference alone is a warning"):
            res = FakeVerifier(doi_hits=[hit("crossref", **LODOCO)]).verify(
                ref(title=LODOCO["title"], first_author="Nidorf", year="2020", journal="JAMA", doi="10.1056/nejmoa2021372"))
            self.assertEqual(res.status, "VERIFIED_WITH_WARNINGS")
            self.assertEqual(sum(1 for c in res.checks if c.outcome == "warning"), 1)
        with self.subTest("volume AND first page both different means a different work (second-round audit)"):
            res = FakeVerifier(doi_hits=[hit("crossref", **LODOCO)]).verify(
                ref(title=LODOCO["title"], first_author="Nidorf", year="2020", journal="JAMA", volume="384", pages="1900-1910", doi="10.1056/nejmoa2021372"))
            self.assertEqual(res.status, "MISMATCH")
            self.assertIn("DIFFERENT_WORK", res.flags)
        with self.subTest("dead DOI, but the title exists elsewhere"):
            nf = [cc.Hit(source="crossref", query="doi:x", ok=True, found=False)]
            cand = {"source": "openalex", "doi": "10.1056/nejmoa2021372", "pmid": "", "title": LODOCO["title"], "first_author": "Nidorf", "year": "2020", "journal": "NEJM"}
            v = FakeVerifier(doi_hits=nf, title_hits=[cc.Hit(source="openalex", query="t", ok=True, candidates=[cand])])
            res = v.verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020", doi="10.1056/nejmoa9999999"))
            self.assertEqual(res.status, "NOT_FOUND")
            self.assertIn("IDENTIFIER_WRONG_BUT_TITLE_EXISTS", res.flags)
        with self.subTest("evidence tier: abstract from PubMed, then full text via the PMCID it reports"):
            ph = hit("pubmed", **LODOCO)
            ph.abstract, ph.pmcid = "BACKGROUND: ...", "PMC1234567"
            body = "<article><body><sec><title>Methods</title><p>" + ("word " * 200) + "</p></sec></body></article>"
            http = mock.Mock()
            http.get.return_value = (200, body, "")
            with mock.patch.object(cc, "pubmed_by_pmid", return_value=ph):
                ev = cc.fetch_evidence_for(http, {"pmid": "32865380", "doi": "10.1056/nejmoa2021372"})
            self.assertEqual((ev["tier"], ev["pmcid"]), ("full_text", "PMC1234567"))
            self.assertIn("PMC1234567/fullTextXML", http.get.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
