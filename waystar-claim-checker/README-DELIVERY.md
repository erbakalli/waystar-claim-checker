# Waystar Claim Checker — Udhëzime përdorimi

Programi punon në Windows dhe kërkon Microsoft Edge të instaluar. Nuk kërkon Python ose Microsoft Excel të instaluar. Për kërkimin live duhet një llogari Waystar me akses te Claims dhe lidhje interneti.

## Hapja

Nëse programi është në ZIP, nxirre të gjithë dosjen përpara hapjes. Mbaji `Waystar Claim Checker.exe` dhe `portal-profile.json` në të njëjtën dosje. Mos ndrysho konfigurimin e portalit pa e verifikuar.

Hape `Waystar Claim Checker.exe` me double-click, ose përdor `Run Claims.cmd`. Nëse është vendosur një shkurtore në Desktop, përdor atë. Hape vetëm një kopje të programit.

## Kontrolli i faturave

1. Kliko **Open Browser**, hyr në Waystar dhe përfundo MFA. Ky buton hap dritaren edhe nëse Edge është tashmë i hapur dhe nuk kërkon një Excel të zgjedhur. Browser-i i automatizimit përdor profilin e vet.
2. Kliko fushën **Receivables Excel** dhe zgjidh raportin `.xlsx`. Raporti duhet të ketë kolonën `Invoice No`, `Invoice Number` ose `InvoiceNumber`. Programi lexon fletën e parë dhe përdor rreshtin e parë me tekst si titujt e kolonave; kolonat e tjera të hyrjes janë opsionale.
3. Për provën e parë, vendos **Claims per batch** në `1` dhe kliko **Start Automation**. Kontrollo rezultatin me të dhënat që shfaqen në Waystar përpara se të vazhdosh me një batch më të madh.
4. Për të ndaluar, kliko **End Automation** dhe prit konfirmimin në dritare përpara mbylljes.

Programi kërkon sipas PCN, duke përdorur numrin e faturës. Ai lexon të dhënat e claim-eve; nuk ndryshon apo dërgon claim-e në portal. Për një raport tjetër, zgjidh skedarin e ri dhe kliko **Start Automation**. Login-i i browser-it ruhet kur ndryshon raporti.

## Rezultatet

Raporti ruhet pranë Excel-it të zgjedhur, në dosjen `<emri i raportit> - Waystar Claims Output`. Path-i i saktë shfaqet në log-un e dritares. Excel-i origjinal nuk ndryshohet.

Fleta **Claims** ka një rresht për faturë. Kur gjenden disa claim-e, ato numërtohen brenda qelizave. Fushat që mungojnë në portal mbeten bosh. Kontrollo kolonat **AutomationStatus** dhe **LastError** për faturat që kërkojnë ndjekje.

Nëse kërkimi ka më shumë se një faqe rezultatesh, fatura shtyhet për kontroll të mëvonshëm; programi nuk eksporton vetëm një pjesë të claim-eve. Një gabim leximi mund të shënohet **Retry later**; kontrollo **LastError** dhe provo faturën përsëri pasi të jetë zgjidhur problemi.

Nëse raporti i daljes është i hapur në Excel, programi ruan një kopje me **Updated** në emër. Nëse ruajtja dështon, shfaqet butoni i rikuperimit të eksportit: mbyll raportin e hapur ose zgjidh problemin e dosjes dhe përdore për të riprovuar ruajtjen. Kontrollo path-in e ri në log.

Kolona **Amount** në output merr vlerën **Invoice Amount** nga rreshti përkatës në input. Nëse kolona mungon ose qeliza është bosh, kërkimi vazhdon dhe për claim-et e gjetura përdoret shuma nga portali. Vlera zero ruhet si zero. **Billed Amount** dhe llogaritjet e pagesave vazhdojnë të përdorin të dhënat nga portali.

Kur kërkimi kthen **Not Found**, rreshti shfaq të dhënat e pacientit dhe faturës nga Excel-i burim: emrin, numrin e faturës, ID-në, DOB, datat, shumën, balancën dhe shënimet që ekzistojnë. **Claim Status**, **Pay. Status** dhe **AutomationStatus** shfaqin **Not Found**. Shënimi tregon se të dhënat vijnë nga input-i; fatura nuk numërohet si claim i gjetur. Fushat e pagesës dhe identifikimit të claim-it nga portali mbeten bosh.

Programi pret përfundimin e kërkimit përpara se të pranojë **Not Found**. Nëse kërkimi dështon ose rezultatet nuk mund të konfirmohen, fatura shënohet **Retry later** dhe arsyeja shfaqet te **LastError**.
Pas këtij përditësimi, rezultatet e versionit të mëparshëm rikërkohen në një ekzekutim të ri; programi nuk i merr Not Found e vjetra si rezultate të konfirmuara nga kontrolli i ri.

## Nëse automatizimi nuk nis

Kontrollo që Edge është i instaluar, që login/MFA ka përfunduar, që raporti është `.xlsx` me kolonën e faturës dhe që `portal-profile.json` është pranë executable-it. Lexo mesazhin në dritare dhe log-un; nëse sesioni ka skaduar, kryej login përsëri.

Nëse programi mbyllet gjatë hapjes, dërgo mesazhin e gabimit dhe, nëse është krijuar, skedarin `startup-error.log` pranë executable-it. Mos përfshi raportet me të dhëna pacientësh ose të dhënat e login-it në kërkesën për mbështetje.
