"""The fixed sentences of the blind Turkish voice test.

Every voice reads these same 16 sentences, in this order, so the scores compare. The last
three (s1, s2, s3) are the seam test: they are asked of a voice as three consecutive
requests, and the clips are joined into one.
"""

SENTENCES: list[tuple[str, str]] = [
    ("n1", "Bugün hava on sekiz derece, yarın yirmi üç dereceye çıkacak."),
    ("n2", "Toplantı on beş Mart iki bin yirmi altı Pazartesi günü saat dokuz otuzda başlıyor."),
    ("n3", "Ürünün fiyatı bin iki yüz elli lira, kargo yüzde on indirimli."),
    ("n4", "Frank Herbert'in romanı Dune, bin dokuz yüz altmış beşte yayımlandı."),
    ("n5", "Apple'ın yeni çipi M beş Ultra, önceki nesilden yüzde kırk daha hızlı."),
    ("i1", "Sabah altıda kalktım, işe gitmeden önce İstanbul'un trafiğini düşündüm."),
    ("i2", "Ilık rüzgâr Ağrı Dağı'nın eteklerinden esiyordu; ışıklar ırmakta parlıyordu."),
    ("a1", "Dr. Smith'e göre bu ilaç kırk yedi milyon hastaya ulaştı."),
    ("d1", "1997'de çıkan 20 oyun arasında en sevdiğim 3. sıradaydı."),
    ("l1", "Heighliner gerçekten çok büyük; bütün fırkateynlerimizi küçük bir köşeye sıkıştırır."),
    ("q1", "Peki bunu nasıl açıklarsınız?"),
    ("e1", "Hayır, hayır, hayır! Bu kesinlikle böyle olmamalıydı."),
    ("c1", "Şirketin CEO'su, NASA ve TÜBİTAK ile ortak bir proje açıkladı."),
    ("s1", "Kitaplarda bile bu gemiler yeterince açıklanmıyor."),
    ("s2", "Tek açıklama ilk kitabın başlarında geçer."),
    ("s3", "Leto, Paul'e bunları anlatırken şöyle der:"),
]

SEAM_IDS = ("s1", "s2", "s3")
