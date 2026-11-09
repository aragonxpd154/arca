"""Testes do motor com um "cartão" simulado que tem setores ruins e instáveis.

Rodar:  python3 -m unittest discover -s tests -v
"""
import errno
import hashlib
import os
import random
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arca import bootinfo, writer  # noqa: E402
from arca.mapfile import BAD, FINISHED, MapFile  # noqa: E402
from arca.rescue import Rescuer  # noqa: E402

SEC = 512


def make_fat16_card(path, sectors=40960):
    """Cria uma imagem com MBR + partição FAT16, como num CF típico."""
    rnd = random.Random(42)
    data = bytearray(rnd.getrandbits(8) for _ in range(sectors * SEC))
    mbr = bytearray(512)
    struct.pack_into("<I", mbr, 440, 0xCAFEBABE)
    psecs = sectors - 63 - 1000  # deixa 1000 setores livres no fim
    entry = struct.pack("<B3sB3sII", 0x80, b"\0\0\0", 0x06, b"\0\0\0", 63, psecs)
    mbr[446:462] = entry
    mbr[510:512] = b"\x55\xAA"
    data[0:512] = mbr
    vbr = bytearray(512)
    vbr[0:3] = b"\xEB\x3C\x90"
    vbr[3:11] = b"MSDOS5.0"
    struct.pack_into("<HBHBHHBHHHII", vbr, 11, 512, 4, 1, 2, 512, psecs, 0xF8, 40, 32, 2, 63, 0)
    vbr[43:54] = b"CARTAO CF  "
    vbr[54:62] = b"FAT16   "
    vbr[510:512] = b"\x55\xAA"
    data[63 * SEC:64 * SEC] = vbr
    with open(path, "wb") as f:
        f.write(data)


class FaultyReader:
    def __init__(self, path, bad, flaky=None, lose_after=None):
        self.f = open(path, "rb")
        self.bad = bad                    # conjunto de setores sempre ruins
        self.flaky = dict(flaky or {})    # setor -> falhas antes de ler
        self.lose_after = lose_after
        self.reads = 0

    def read(self, pos, size):
        self.reads += 1
        if self.lose_after is not None and self.reads > self.lose_after:
            raise OSError(errno.ENODEV, "No such device")
        first, last = pos // SEC, (pos + size - 1) // SEC
        for s in range(first, last + 1):
            if s in self.bad:
                raise OSError(errno.EIO, "Input/output error")
            if self.flaky.get(s, 0) > 0:
                self.flaky[s] -= 1
                raise OSError(errno.EIO, "Input/output error")
        self.f.seek(pos)
        return self.f.read(size)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = os.path.join(self.d, "card.raw")
        make_fat16_card(self.src)
        self.total = os.path.getsize(self.src)
        self.img = os.path.join(self.d, "out.img")
        self.map = self.img + ".map"

    def test_full_recovery_with_bad_and_flaky(self):
        bad = {500, 501, 502, 9000, 30000}
        flaky = {12345: 2, 20000: 1}
        events = []
        r = Rescuer(FaultyReader(self.src, bad, flaky), self.total, self.img, self.map,
                    retries=3, emit=events.append)
        res = r.run()
        r.close()
        self.assertEqual(res["status"], "done")
        m = MapFile.load(self.map, self.total)
        bad_secs = {p // SEC + k for p, s, _ in m.intervals({BAD}) for k in range(s // SEC)}
        self.assertEqual(bad_secs, bad)          # instáveis foram recuperados
        self.assertEqual(m.count(FINISHED), self.total - len(bad) * SEC)
        src, out = open(self.src, "rb").read(), open(self.img, "rb").read()
        for s in range(self.total // SEC):
            if s not in bad:
                self.assertEqual(src[s * SEC:(s + 1) * SEC], out[s * SEC:(s + 1) * SEC], s)
        kinds = {e["ev"] for e in events}
        self.assertTrue({"start", "progress", "map", "boot", "finished"} <= kinds | {"finished"})
        boot = [e for e in events if e["ev"] == "boot"][-1]["info"]
        self.assertEqual(boot["scheme"], "MBR")
        self.assertEqual(boot["partitions"][0]["fs"]["fs"], "FAT16")
        self.assertEqual(boot["partitions"][0]["fs"]["label"], "CARTAO CF")

    def test_resume_after_device_lost(self):
        r = Rescuer(FaultyReader(self.src, {700}, lose_after=60), self.total, self.img, self.map,
                    retries=1)
        res = r.run()
        r.close()
        self.assertEqual(res["status"], "lost")
        done_before = MapFile.load(self.map, self.total).count(FINISHED)
        self.assertGreater(done_before, 0)
        r = Rescuer(FaultyReader(self.src, {700}), self.total, self.img, self.map, retries=1)
        self.assertTrue(r.resumed)
        res = r.run()
        r.close()
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["bad"], SEC)

    def test_mapfile_merge(self):
        m = MapFile(10000)
        m.set(0, 5000, FINISHED)
        m.set(5000, 5000, FINISHED)
        self.assertEqual(m.as_list(), [[0, 10000, "+"]])
        m.set(100, 10, BAD)
        m.set(110, 10, BAD)
        self.assertEqual(m.as_list(), [[0, 100, "+"], [100, 20, "-"], [120, 9880, "+"]])

    def test_write_and_verify(self):
        target = os.path.join(self.d, "novo.raw")
        with open(target, "wb") as f:
            f.truncate(self.total + 10 * SEC)
        evs = []
        res = writer.write_image(self.src, target, evs.append, verify=True)
        self.assertTrue(res["match"])
        self.assertEqual(res["sha_img"], hashlib.sha256(open(self.src, "rb").read()).hexdigest())

    def test_fit_check(self):
        info = bootinfo.analyze(lambda p, s: open(self.src, "rb").read()[p:p + s], self.total)
        self.assertEqual(writer.fit(self.total, self.total - SEC, info)["level"], "smaller_ok")
        self.assertEqual(writer.fit(self.total, self.total + SEC, info)["level"], "larger")
        self.assertEqual(writer.fit(self.total, self.total - 2000 * SEC, info)["level"], "too_small")


if __name__ == "__main__":
    unittest.main()
