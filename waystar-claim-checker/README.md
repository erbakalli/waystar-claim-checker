# Waystar Claim Checker

Programi perdor vetem automatizimin live te Waystar.

## Nisja

Hap Run Claims.cmd. Ai nis dist/Waystar Claim Checker.exe.
Nuk kerkohet Python per executable-in. Microsoft Edge duhet te jete i instaluar.

1. Zgjidh workbook-un te Receivables Excel.
2. Kliko Open Browser dhe kryej login/MFA ne Waystar.
3. Kliko Start Automation. End Automation ndalon ne menyre te sigurt.

Programi kerkon Invoice No ne menyren PCN dhe lexon te gjitha claim-et e kthyera.
Kerkimet e njepasnjeshme perdorin search-in lart ne te njejten faqe rezultatesh.
Faqja e integrimit hapet ne fillim ose gjate rikuperimit kur search-i mungon.
Programi pret ndryshimin e rezultateve perpara leximit; rezultatet e vjetra nuk pranohen.
Rezultatet duhet te jene te qendrueshme perpara leximit. Gabimet e leximit riprovohen deri ne 3 here.
Nese nje fature nuk lexohet, mbetet Retry later dhe gabimi shfaqet te LastError;
programi vazhdon me faturat e tjera. Probleme te perseritura te sesionit mund te kerkojne ndalim ose login.
Nuk ndryshon apo dergon claim-e ne portal.
Raporti ruhet prane Excel-it ne <source name> - Waystar Claims Output.
Nese raporti eshte i hapur ne Excel, ruhet nje kopje e re me Updated ne emer.
Programi shfaq path-in e sakte te raportit te ri ne log-un e dritares.
Claims ka 34 kolona sipas modelit, nje rresht per fature dhe claim-e te numertuara brenda qelizave.
Fushat pa te dhena mbeten bosh. Excel-i origjinal dhe Comments ruhen.
Office lexohet nga emri prane Account ne fillim (pa numrin e llogarise)
dhe vendoset njesoj vetem ne rreshtat e perpunuar, edhe kur nuk gjenden claim-e.
Contract vendoset NGS vetem ne rreshtat e perpunuar te Claims.
Office dhe Contract mbeten bosh ne rreshtat ende ne pritje dhe plotesohen kur perpunohen.
Claims ka vija ndarese te holla gri rreth cdo qelize, perfshire qelizat bosh.
Kur nje fature ka te pakten nje claim me status Suspense, AutomationStatus shfaq Pending.
VisitDate tregon periudhen Stmt Start - Stmt End ne formatin MM/DD/YYYY - MM/DD/YYYY.
Visit/Supply/Expense merr Total Charges nga Waystar.
InvoiceNumber merr PCN nga portali; pa rezultate nga portali mbetet bosh.
Invoice Date merr Received Date. Amount merr Total Charges.
PaidAmount dhe Paid Amount marrin Provider Reimb; mungesa e shumes mbetet bosh.
Adj. dhe Balance jane Total Charges - Provider Reimb; kur mungon nje shume mbeten bosh.
Pay. Status merr Claim Status dhe TRN merr DCN nga portali, duke ruajtur zerot ne fillim.
AltPatientID merr Beneficiary ID; StartDate dhe EndDate marrin Stmt Start dhe Stmt End.
Claim ID merr DCN; Billed Amount merr Total Charges dhe Claim Status merr statusin e portalit.
Denial Reason merr pershkrimin e Primary Reason, jo kodin numerik.
Finalized Date merr Paid Date nga portali; pa Paid Date mbetet bosh.

## Skedaret

- dist: executable-i live, portal-profile.json dhe udhezimet e perdorimit.
- src dhe main.py: kodi i automatizimit live.
- portal-profile.json: konfigurimi aktiv i Waystar; duhet prane executable-it.
- ../Kantime-engine-main/src: engine-i lokal i perdorur nga programi.
- .venv: varesite per nisjen nga Python dhe rindertim.
- tests: kontrollet e kerkimit live, eksportit dhe ruajtjes se te dhenave.
- .kantime-build: vetem burimet Tcl/Tk te nevojshme per rindertim.
- Waystar Claim Checker.spec, pyproject.toml, kantime.toml: konfigurimi i build-it.

## Mirembajtja

Nga kjo dosje:

    .\.venv\Scripts\python.exe main.py --smoke-test
    .\.venv\Scripts\python.exe -m unittest discover -s tests -v
    .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean "Waystar Claim Checker.spec"

Pas rindertimit, kopjo portal-profile.json ne dist.
Kontrollet automatike perdorin te dhena testimi; verifikimi ne portal kerkon login-in tend.
Per nje prove reale fillestare, vendos Claims per batch ne 1.
