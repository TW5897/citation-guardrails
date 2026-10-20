"""Regression tests for the second-round audit findings. Offline."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "check-references" / "scripts" / "citecheck.py"
sys.path.insert(0, str(SCRIPT.parent))
sys.path.insert(0, str(ROOT / "tests"))

import citecheck as cc  # noqa: E402
from test_citecheck import FakeVerifier, LODOCO, hit, ref  # noqa: E402

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
VANC = [
    "Nidorf SM, Fiolet ATL, Mosterd A, et al. Colchicine in Patients with Chronic Coronary Disease. N Engl J Med. 2020;383(19):1838-1847. doi:10.1056/NEJMoa2021372",
    "Smith J, Jones A. Intravenous Turmeric and 30-Day Mortality in Cardiogenic Shock: The TURMERIC-SHOCK Randomized Trial. Lancet. 2023;401(10380):1120-1130.",
    "Ebrahimi S, Khan A, Patel R. Colchicine in Patients with Chronic Coronary Disease. N Engl J Med. 2020;383(19):1838-1847.",
]


class ListShapes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_single_numbered_item_keeps_author(self):
        p = self.tmp / "one.txt"
        p.write_text("1. " + VANC[2] + "\n", encoding="utf-8")
        refs = cc.parse_input(p)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].first_author, "Ebrahimi")
        self.assertEqual(refs[0].ref_id, "ref_1")

    def test_lancet_style_numbering(self):
        for sep in (" ", "\t"):
            p = self.tmp / "lancet.txt"
            p.write_text("References\n\n" + "\n".join(f"{i}{sep}{r}" for i, r in enumerate(VANC, 1)) + "\n", encoding="utf-8")
            refs = cc.parse_input(p)
            self.assertEqual([r.first_author for r in refs], ["Nidorf", "Smith", "Ebrahimi"], sep.encode())

    def test_docx_line_breaks_and_endnotes(self):
        body = '<w:p><w:r><w:t>References</w:t></w:r></w:p><w:p><w:r><w:t>' + '</w:t><w:br/><w:t>'.join(VANC) + '</w:t></w:r></w:p>'
        p = self.tmp / "br.docx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{body}</w:body></w:document>')
        self.assertEqual([r.first_author for r in cc.parse_input(p)], ["Nidorf", "Smith", "Ebrahimi"])
        endn = (f'<?xml version="1.0" encoding="UTF-8"?><w:endnotes {W}><w:endnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:endnote>'
                f'<w:endnote w:id="1"><w:p><w:r><w:t>{VANC[0]}</w:t></w:r></w:p></w:endnote><w:endnote w:id="2"><w:p><w:r><w:t>{VANC[1]}</w:t></w:r></w:p></w:endnote></w:endnotes>')
        body2 = '<w:p><w:r><w:t>Colchicine reduced events.</w:t></w:r><w:r><w:endnoteReference w:id="1"/></w:r><w:r><w:t> Turmeric did not.</w:t></w:r><w:r><w:endnoteReference w:id="2"/></w:r></w:p>'
        p2 = self.tmp / "endnotes.docx"
        with zipfile.ZipFile(p2, "w") as z:
            z.writestr("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{body2}</w:body></w:document>')
            z.writestr("word/endnotes.xml", endn)
        refs = cc.parse_input(p2)
        self.assertEqual([r.first_author for r in refs], ["Nidorf", "Smith"])
        self.assertEqual([c["cites"] for c in cc.extract_claims(cc.read_text(p2))], [["1"], ["2"]])

    def test_wrapped_doi_rejoined(self):
        p = self.tmp / "wrap.txt"
        p.write_text("References\n\n[30] Ridker PM, MacFadyen JG. Effect of interleukin-1 inhibition with canakinumab on incident lung cancer. Lancet. 2017;390:1833-1842. doi:10.1016/S0140-\n6736(17)32247-X\n[31] " + VANC[0] + "\n", encoding="utf-8")
        refs = cc.parse_input(p)
        self.assertEqual(refs[0].doi, "10.1016/s0140-6736(17)32247-x")

    def test_bare_sources_heading_is_not_a_reference_list(self):
        p = self.tmp / "prose.md"
        p.write_text("# Methods\n\n## Sources\n\nWe used the Plymouth registry (2024) with 141 patients and extracted the data we needed for the analysis of outcomes.\n\n"
                     "1. Patients were included if they had an implantable loop recorder inserted during the study period.\n2. Patients were excluded if they had a pacemaker.\n\n# References\n\n1. " + VANC[0] + "\n", encoding="utf-8")
        refs = cc.parse_input(p)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].first_author, "Nidorf")

    def test_numbered_body_list_without_heading_is_ignored(self):
        p = self.tmp / "body.md"
        p.write_text("Objectives\n\n1. To measure thirty-day mortality among patients in the registry cohort.\n2. To compare outcomes between the two treatment groups over time.\n", encoding="utf-8")
        self.assertEqual(cc.parse_input(p), [])


class Styles(unittest.TestCase):
    def test_book_chapter(self):
        r = cc.parse_free_citation("Kubben P. Data Sources. In: Kubben P, Dumontier M, Dekker A, editors. Fundamentals of Clinical Data Science. Cham: Springer; 2019. p. 3-9. doi:10.1007/978-3-319-99713-1_1", "r")
        self.assertTrue(r.is_chapter)
        self.assertEqual(r.title, "Data Sources")
        self.assertEqual(r.first_author, "Kubben")
        self.assertEqual(r.year, "2019")
        self.assertIn("Fundamentals of Clinical Data Science", r.journal)

    def test_harvard_and_mla(self):
        h = cc.parse_free_citation("Tardif, J.C. et al. (2019) 'Efficacy and safety of low-dose colchicine after myocardial infarction', New England Journal of Medicine, 381(26), pp. 2497–2505.", "r")
        self.assertEqual((h.first_author, h.year, h.volume), ("Tardif", "2019", "381"))
        self.assertTrue(h.title.lower().startswith("efficacy and safety"))
        self.assertFalse(h.style_unrecognised)
        m = cc.parse_free_citation('Tardif, Jean-Claude, et al. "Efficacy and Safety of Low-Dose Colchicine after Myocardial Infarction." New England Journal of Medicine, vol. 381, no. 26, 2019, pp. 2497-2505.', "r")
        self.assertEqual((m.first_author, m.year), ("Tardif", "2019"))
        self.assertTrue(m.title.startswith("Efficacy and Safety"))

    def test_nlm_group_after_semicolon(self):
        r = cc.parse_free_citation("Tardif JC, Kouz S, Waters DD, et al.; COLCOT Investigators. Efficacy and Safety of Low-Dose Colchicine after Myocardial Infarction. N Engl J Med. 2019 Dec 26;381(26):2497-2505.", "r")
        self.assertEqual(r.first_author, "Tardif")
        self.assertTrue(r.title.startswith("Efficacy and Safety"))

    def test_curly_apostrophe_and_dotted_initials(self):
        self.assertTrue(cc.surnames_match("O’Donnell", "O'Donnell"))
        self.assertTrue(cc.surnames_match("D’Agostino", "D'Agostino"))
        self.assertEqual(cc.surname_from_display("Nidorf S.M."), "Nidorf")
        self.assertEqual(cc.surname_from_display("Tardif J.-C."), "Tardif")
        r = cc.parse_free_citation("Nidorf S.M., Fiolet A.T.L., Mosterd A. Colchicine in Patients with Chronic Coronary Disease. N Engl J Med. 2020;383(19):1838-1847.", "r")
        self.assertEqual(r.first_author, "Nidorf")

    def test_unrecognised_layout_is_unverifiable_not_not_found(self):
        r = cc.Ref(ref_id="r", title="'Something odd', New England Journal of Medicine, 381(26), pp", raw="x y z w", style_unrecognised=True)
        res = FakeVerifier(title_hits=[cc.Hit(source="openalex", query="t", ok=True), cc.Hit(source="europepmc", query="t", ok=True)], sources=("openalex", "europepmc")).verify(r)
        self.assertEqual(res.status, "UNVERIFIABLE")
        self.assertIn("LAYOUT_NOT_RECOGNISED", res.flags)

    def test_extra_pmids_recorded(self):
        r = cc.parse_free_citation(VANC[0] + " PMID: 32865380. PMID: 31733140", "r")
        self.assertEqual(r.pmid, "32865380")
        self.assertEqual(r.extra_pmids, ["31733140"])

    def test_shell_split_doi_rejoined(self):
        self.assertEqual(cc.rejoin_shell_split_dois(["10.1016/S0140-6736", "20", "31180-6", "32865380"]), ["10.1016/S0140-6736(20)31180-6", "32865380"])


class VerifyRulesV3(unittest.TestCase):
    def test_sibling_paper_frankenstein_is_mismatch(self):
        leader = dict(LODOCO, title="Liraglutide and Cardiovascular Outcomes in Type 2 Diabetes", authors=["Marso", "Daniels"], year="2016", volume="375", pages="311-322", doi="10.1056/nejmoa1603827")
        res = FakeVerifier(doi_hits=[hit("crossref", **leader), hit("openalex", **leader)]).verify(
            ref(title="Semaglutide and Cardiovascular Outcomes in Patients with Type 2 Diabetes", first_author="Marso", year="2016", journal="N Engl J Med", volume="375", pages="1834-1844", doi="10.1056/nejmoa1603827"))
        self.assertEqual(res.status, "MISMATCH")
        self.assertIn("DIFFERENT_WORK", res.flags)

    def test_vernacular_title_matches(self):
        h = hit("pubmed", **dict(LODOCO, title="[Diagnosis and treatment of pulmonary embolism]."))
        h.alt_titles = ["Diagnostik und Therapie der Lungenembolie"]
        res = FakeVerifier(doi_hits=[h]).verify(ref(title="Diagnostik und Therapie der Lungenembolie", first_author="Nidorf", year="2020", doi="10.1056/nejmoa2021372"))
        self.assertEqual(res.status, "VERIFIED")

    def test_title_match_keeps_candidate_record(self):
        cand = {"source": "openalex", "doi": "", "pmid": "", "title": "Diagnostik und Therapie der Lungenembolie", "first_author": "Konstantinides", "year": "2021", "journal": "Herz"}
        th = cc.Hit(source="openalex", query="t", ok=True, candidates=[cand])
        v = FakeVerifier(title_hits=[th, cc.Hit(source="europepmc", query="t", ok=True)], sources=("openalex", "europepmc"))
        res = v.verify(ref(title="Diagnostik und Therapie der Lungenembolie", first_author="Konstantinides", year="2021", journal="Herz"))
        self.assertEqual(res.status, "VERIFIED")


class CliV3(unittest.TestCase):
    def test_claims_records_relative_path(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "qc"
            p = subprocess.run([sys.executable, str(SCRIPT), "claims", str(ROOT / "examples" / "manuscript-audit" / "manuscript.md"), "--out-dir", str(out)], capture_output=True, text=True, cwd=str(ROOT))
            self.assertEqual(p.returncode, 0, p.stderr)
            d = json.loads((out / "claims_to_check.json").read_text(encoding="utf-8"))
            self.assertNotIn(str(Path.home()), d["manuscript"])

    def test_audit_records_carry_vancouver_string(self):
        v = FakeVerifier(doi_hits=[hit("crossref", **LODOCO)])
        res = v.verify(ref(title=LODOCO["title"], first_author="Nidorf", year="2020", doi="10.1056/nejmoa2021372"))
        audit = cc.build_audit([res], "ms.md", {"sources": ["crossref"]})
        self.assertTrue(audit["records"][0]["vancouver"].startswith("Nidorf"))


if __name__ == "__main__":
    unittest.main()
