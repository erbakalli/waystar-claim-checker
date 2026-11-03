## Agency-independent GUI

Agency selection has been removed. The application accepts workbooks from any
agency; Office is taken from each input row's Office, Agency, Agency Name or
Office Name column and stays blank if it is unavailable.

Payer selection has also been removed. The app detects a visible Claims search
on either supported integration. Open the Claims portal you want to check before
starting. When more than one candidate is visible, it stops instead of guessing.
Choose the worksheet when prompted if more than one Medicare/NGS sheet matches.
Contract values come from the input Contract, Payer or Insurance column; a payer
is never inferred from the portal hostname. A new browser starts at the standard
Claims integration; navigate inside Waystar to the Claims portal you need.

# Waystar Claim Checker

Programi perdor vetem automatizimin live te Waystar.

## Nisja

Hap Run Claims.cmd. Ai nis dist/Waystar Claim Checker.exe.
Nuk kerkohet Python per executable-in. Microsoft Edge duhet te jete i instaluar.
Te Browser zgjidh njeren nga dy menyrat:
- Separate automation window (zgjedhja fillestare): hap Edge te programit dhe kryen login ne Waystar.
- Use my open Edge: lidhet me Edge ku punon zakonisht dhe perdor sesionin ekzistues.
  Ne ate Edge hap edge://inspect/#remote-debugging dhe aktivizo
  Allow remote debugging for this browser instance. Mbaje Edge hapur.
  Kur klikon Open Browser, prano lidhjen nese Edge shfaq kerkesen.
  Mjafton nje klikim: tab-i shfaqet menjehere dhe programi pret search-in ose login-in.
  Kur SSO vonohet, programi riprovon vete ne te njejtin tab; nuk nevojiten klikime te perseritura.
  Lidhja ekzistuese ruhet dhe nje tab login-i Waystar i hapur perdoret perseri.
  Nese lidhja nuk eshte aktivizuar, programi tregon udhezimin pa hapur profil tjeter.
  Mbyllja e programit le Edge dhe tab-et e punes hapur.

1. Zgjidh workbook-un te Receivables Excel.
   Sheeti zbulohet nga emri (NGS ose Medicare); kur ka disa perputhje, zgjidhet manualisht.
   NGS njeh p.sh. NGS, ngs, NGS Payer; HH - Medicare njeh Medicare, HH,
   Home Health dhe kombinime si Medicare Home Health, pavaresisht shkronjave dhe ndareseve.
   Kur nuk ka perputhje ose ka disa, programi kerkon zgjedhjen manuale te sheet-it.
   Cancel ndalon pergatitjen pa nisur kerkimin e claim-eve.
   Vetem Invoice No / Invoice Number / InvoiceNumber eshte kolone e detyrueshme.
   Insurance ID, datat, Invoice Amount dhe Comments jane opsionale; kerkimi perdor PCN.
   Ne rezultatet e portalit, Beneficiary ID, DCN dhe Claim Status mund te jene bosh.
   Rreshtat pranohen kur PCN perputhet me kerkimin; fushat qe mungojne mbeten bosh ne output.
2. Kliko Open Browser dhe kryej login/MFA ne Waystar.
   NGS hap integrimin tabappid=87 (claimseditor.ecorpnet.com).
   HH - Medicare hap integrimin tabappid=85 (analytics.ecorpnet.com).
   Portali zbulohet nga faqja Claims e hapur; Office merret nga rreshti ne input.
   Kjo lidhje nuk ndryshon automatikisht Account-in ne Waystar.
   Open Browser punon edhe pa Excel te zgjedhur; perdor tab-in Waystar te hapur
   ose hap nje tab te ri ne Edge ekzistues. Tab-et e tjera nuk ndryshohen.
   Excel-i pergatitet kur klikon Start Automation dhe login-i ruhet kur ndryshon raporti.
3. Kliko Start Automation. End Automation ndalon ne menyre te sigurt.

Programi kerkon Invoice No ne menyren PCN dhe lexon te gjitha claim-et e kthyera.
Kerkimet e njepasnjeshme perdorin search-in lart ne te njejten faqe rezultatesh.
Nisja perdor faqen Claims ekzistuese kur search-i eshte gati, edhe kur ndryshon raporti.
Faqja e integrimit rihapet vetem kur search-i mungon ose duhet rikuperim.
Log-u tregon pergatitjen e search-it, leximin e Account dhe nisjen e faturave.
Programi pret ndryshimin e rezultateve perpara leximit; rezultatet e vjetra nuk pranohen.
Kur kerkimi hap nje dokument te ri, rezultatet lexohen pasi dokumenti eshte parsuar
dhe pergjigjja HTTP eshte e suksesshme, edhe kur njoftimi requestfinished vonohet.
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
Contract merr payer-in e zgjedhur vetem ne rreshtat e perpunuar te Claims.
Office dhe Contract mbeten bosh ne rreshtat ende ne pritje dhe plotesohen kur perpunohen.
Claims ka vija ndarese te holla gri rreth cdo qelize, perfshire qelizat bosh.
Kur portali tregon Suspense, Claim Status dhe Pay. Status ne output shfaqin PENDING. AutomationStatus tregon gjendjen e automatizimit.
VisitDate tregon periudhen Stmt Start - Stmt End ne formatin MM/DD/YYYY - MM/DD/YYYY.
Visit/Supply/Expense merr Total Charges nga Waystar.
InvoiceNumber merr PCN nga portali kur gjenden claim-e.
Kur kerkimi kthen Not Found, Claims shfaq te dhenat e pacientit dhe fatures nga input-i:
emrin, Invoice No, Insurance ID / Patient ID, DOB, datat, shumen, balancen dhe shenimet qe jane te pranishme.
Claim Status, Pay. Status dhe AutomationStatus tregojne Not Found.
Note(s)/ Follow Up sqaron qe keto te dhena vijne nga input-i; fatura nuk numerohet si claim i gjetur.
Fushat e pageses dhe identifikimit te claim-it nga portali mbeten bosh.
Invoice Date merr Received Date / Original Rec Date nga portali; kur mungon, perdor Invoice Date nga input-i.
DOB merret nga DOB / Date of Birth / Birth Date ne input, edhe per claim-et e perfunduara.
Eksporti ploteson edhe datat bosh ne rezultatet e ruajtura, pa rikerkuar claim-et.
Amount merr Total Charges.
PaidAmount dhe Paid Amount marrin Provider Reimb; mungesa e shumes mbetet bosh.
Adj. dhe Balance jane Total Charges - Provider Reimb; kur mungon nje shume mbeten bosh.
Pay. Status merr Claim Status dhe TRN merr DCN nga portali, duke ruajtur zerot ne fillim.
AltPatientID merr Beneficiary ID; StartDate dhe EndDate marrin Stmt Start dhe Stmt End.
Claim ID merr DCN; Billed Amount merr Total Charges dhe Claim Status merr statusin e portalit.
Denial Reason merr pershkrimin e Primary Reason, jo kodin numerik.
Primary Reason is detected automatically from each claim cell, independent of
payer, agency or portal host. A recognized reason.aspx popup link takes priority:
the checker clicks it, verifies the matching reason code, reads the description,
clicks X and waits for the popup to close. Cells without a popup target use a
native title or hover tooltip. Standard role=tooltip and Telerik RadToolTip
surfaces are detected automatically; the configured tooltip selector remains
available for portal-specific tooltips. Each row is checked again so mixed grids
work without reusing a previous claim's tooltip. A broken popup, ambiguous targets
or an unverified description fail the invoice instead of exporting the code or
silently substituting a tooltip. Neither payer nor agency selection is required for reason detection.
Per cdo Invoice No lexohen te gjitha rreshtat e kthyer dhe faqet pasuese te grid-it,
pastaj kerkohet fatura tjeter. Log-u tregon pritjen e kerkimit dhe Primary Reason per cdo claim.
Nese nje faqe ose reason nuk lexohet, fatura nuk ruhet si e perfunduar me rezultate te pjesshme.
Finalized Date merr Paid Date nga portali; pa Paid Date mbetet bosh.

## Resume nga Excel

`AutomationStatus` dhe `LastError` perdoren per vazhdimin e automatizimit;
statusi i claim-it mbahet te `Claim Status` (`Suspense` eksportohet si `PENDING`).

- `Done`: kontrolli perfundoi, perfshire `Not Found`; rreshti nuk rikerkohet.
- `Skipped`: rreshti eshte anashkaluar; nuk rikerkohet.
- `Retry later`, `Pending`, `In Progress`, status bosh ose i panjohur: rreshti kontrollohet perseri.
- `LastError` me tekst: rreshti riprovohet edhe nese `AutomationStatus` eshte `Done` ose `Skipped`.
  Pas nje kontrolli te suksesshem, `LastError` pastrohet.

Per te vazhduar ne te njejtin kompjuter, zgjidh input-in e pandryshuar dhe prano
`Resume saved run` kur shfaqet. Progresi ruhet pas cdo fature.
Mund te zgjedhesh edhe raportin e daljes si `Receivables Excel`: programi lexon
kolonat nga fleta `Claims` (zgjidhe manualisht kur kerkohet), ruan rezultatet e perfunduara dhe kontrollon
vetem rreshtat e mbetur. Kjo funksionon edhe pa databazen lokale te ekzekutimit te pare.
Per te rikerkuar nje rresht nga raporti, pastro `AutomationStatus` ose vendos `Retry later`.
Input-i nuk ndryshohet; programi gjeneron nje raport te ri.

Raportet e vjetra me `AutomationStatus = Pending` ose `Not Found` trajtohen si
te paperfunduara dhe rikontrollohen; vetem statuset teknike `Done` / `Skipped`
pa `LastError` lejojne anashkalimin.

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
