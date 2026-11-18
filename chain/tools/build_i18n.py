#!/usr/bin/env python3
"""Builds node/web/i18n.js: UI language by visitor timezone (ar / fr / en), same runtime as the exchange."""
import json, pathlib
here = pathlib.Path(__file__).parent
D = {
"المستكشف":("Explorateur","Explorer"),"محفظتي":("Mon portefeuille","My wallet"),
"ث":("s","s"),"د":("min","min"),"س":("h","h"),"يوم":("j","d"),"حاسوب عادي":("Ordinateur ordinaire","Ordinary computer"),
"ابحث: رقم كتلة، عنوان، أو معرّف تحويل":("Rechercher : numéro de bloc, adresse ou identifiant de transfert","Search: block number, address or transfer ID"),
"الكتل":("Blocs","Blocks"),"المعروض الكلي":("Offre totale","Total supply"),"باقي مخزون التعدين":("Réserve de minage restante","Mining pool left"),
"المحروق":("Brûlé","Burned"),"مكافأة الكتلة القادمة":("Récompense du prochain bloc","Next block reward"),"تحويلات تنتظر":("Transferts en attente","Pending transfers"),
"الكتلة":("Bloc","Block"),"النوع":("Type","Type"),"المعدّن":("Mineur","Miner"),"تحويلات":("Transferts","Transfers"),"منذ":("Il y a","Ago"),
"النوع:":("Type :","Type:"),"الوقت":("Heure","Time"),"السابقة":("Précédent","Previous"),"عدّن في المرحلة الأولى":("Miné en phase 1","Mined in phase 1"),"الوقت:":("Heure :","Time:"),"السابقة:":("Précédent :","Previous:"),
"من":("De","From"),"إلى":("À","To"),"إلى:":("À :","To:"),"المبلغ":("Montant","Amount"),"المبلغ:":("Montant :","Amount:"),
"الرسوم":("Frais","Fee"),"الرسوم:":("Frais :","Fee:"),"لا تحويلات":("Aucun transfert","No transfers"),
"العنوان":("Adresse","Address"),"عدّن في المرحلة الأولى:":("Miné en phase 1 :","Mined in phase 1:"),
"إرسال":("Envoi","Send"),"استلام":("Réception","Receive"),"تعدين":("Minage","Mining"),"لا شيء بعد":("Rien pour l'instant","Nothing yet"),
"التحويل":("Transfert","Transfer"),"ينتظر التأكيد":("En attente de confirmation","Waiting for confirmation"),"مؤكد في الكتلة":("Confirmé dans le bloc","Confirmed in block"),
"افتح الصفحة عبر":("Ouvrez la page via","Open the page over"),"محفظة جديدة":("Nouveau portefeuille","New wallet"),
"اختر كلمة سر لهذا الهاتف. ثم احفظ «مفتاح الاسترجاع» في مكان آمن: من يملكه يملك المال.":("Choisissez un mot de passe pour ce téléphone. Gardez ensuite la « clé de récupération » en lieu sûr : qui la possède possède l'argent.","Choose a password for this phone. Then keep the “recovery key” somewhere safe: whoever has it owns the money."),
"كلمة سر (8 أحرف على الأقل)":("Mot de passe (8 caractères minimum)","Password (at least 8 characters)"),"إنشاء محفظة":("Créer un portefeuille","Create wallet"),
"أو الصق مفتاح الاسترجاع (64 حرفاً)":("Ou collez la clé de récupération (64 caractères)","Or paste the recovery key (64 characters)"),
"استرجاع محفظة":("Restaurer un portefeuille","Restore wallet"),"كلمة السر قصيرة":("Mot de passe trop court","Password too short"),
"تم إنشاء محفظتك":("Votre portefeuille est créé","Your wallet is created"),
"مفتاح الاسترجاع (انسخه واحفظه، لن يظهر مرة أخرى):":("Clé de récupération (copiez-la et gardez-la, elle ne sera plus affichée) :","Recovery key (copy and keep it, it will not be shown again):"),
"حفظته، تابع":("Je l'ai gardée, continuer","Saved it, continue"),"مفتاح غير صحيح":("Clé invalide","Invalid key"),"كلمة السر":("Mot de passe","Password"),
"فتح المحفظة":("Ouvrir le portefeuille","Open wallet"),"كلمة سر خاطئة":("Mot de passe incorrect","Wrong password"),
"عنواني (للاستلام وللتعدين)":("Mon adresse (pour recevoir et miner)","My address (for receiving and mining)"),"نسخ العنوان":("Copier l'adresse","Copy address"),
"السجل":("Historique","History"),"عنوان المستلم":("Adresse du destinataire","Recipient address"),
"نصفها للمعدّن ونصفها يُحرق.":("La moitié va au mineur, l'autre moitié est brûlée.","Half goes to the miner, half is burned."),
"مبلغ غير صحيح":("Montant invalide","Invalid amount"),"أُرسل.":("Envoyé.","Sent."),"تتبّع التحويل":("Suivre le transfert","Track transfer"),
"التعدين بحاسوبك":("Minez avec votre ordinateur","Mine with your computer"),"فكّ الضغط، ثم شغّل":("Décompressez, puis lancez","Unzip, then run"),"والصق عنوانك.":("et collez votre adresse.","and paste your address."),
"حمّل برنامج التعدين لويندوز":("Télécharger le mineur pour Windows","Download the Windows miner"),
"فكّ الضغط، ثم شغّل start-mining.bat والصق عنوانك.":("Décompressez, lancez start-mining.bat et collez votre adresse.","Unzip, run start-mining.bat and paste your address."),
}
E = [["^insufficient balance$","الرصيد غير كافٍ","Solde insuffisant"],["^bad address$","عنوان غير صحيح","Adresse invalide"],
     ["^bad address checksum$","عنوان غير صحيح","Adresse invalide"],["^bad recipient address$","عنوان المستلم غير صحيح","Adresse du destinataire invalide"],
     ["^fee below minimum 0\\.01 ALTF$","الرسوم أقل من الحد الأدنى 0.01 ALTF","Frais inférieurs au minimum de 0,01 ALTF"],
     ["^bad nonce: want (\\d+)$","رقم التحويل غير صحيح، أعد المحاولة","Numéro de transfert incorrect, réessayez"],
     ["^bad signature$","توقيع غير صحيح","Signature invalide"],["^memo too long$","الملاحظة طويلة","Note trop longue"],
     ["^mempool full$","الشبكة مزدحمة، أعد المحاولة","Réseau saturé, réessayez"],["^already known$","هذا التحويل مُرسل مسبقاً","Transfert déjà envoyé"],
     ["^not found$","غير موجود","Introuvable"],["^block not found$","الكتلة غير موجودة","Bloc introuvable"],["^no such height$","الكتلة غير موجودة","Bloc introuvable"]]
rt = (here / "rt.txt").read_text(encoding="utf-8")
rt = rt[rt.index('"""') + 3: rt.rindex('"""')]
data = {"D": {k: list(v) for k, v in D.items()}, "F": {}, "E": E}
out = here.parent / "node" / "web" / "i18n.js"
out.write_text(rt.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":"))).strip() + "\n", encoding="utf-8")
print(out, out.stat().st_size)
