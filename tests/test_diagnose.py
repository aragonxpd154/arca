"""Testes do diagnóstico com cartões simulados: saudável, corrompido, falsificado, instável…"""
import errno
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arca.diagnose import Diagnoser  # noqa: E402

BLK = 64 * 1024
TOTAL = 32 * BLK  # 2 MiB


class FakeCard:
    def __init__(self, total=TOTAL, real=None, corrupt_block=None, corrupt_from_pass=1,
                 lostw_block=None, unstable_block=None, rerr_block=None, die_after=None):
        self.total = total
        self.real = real or total             # capacidade física real (cartão falso)
        self.mem = bytearray(os.urandom(self.real))
        self.corrupt_block, self.corrupt_from = corrupt_block, corrupt_from_pass
        self.lostw_block, self.unstable_block, self.rerr_block = lostw_block, unstable_block, rerr_block
        self.die_after, self.ops, self.writes_seen = die_after, 0, {}

    def _tick(self):
        self.ops += 1
        if self.die_after is not None and self.ops > self.die_after:
            raise OSError(errno.ENODEV, "No such device")

    def write(self, pos, data):
        self._tick()
        blk = pos // BLK
        self.writes_seen[blk] = self.writes_seen.get(blk, 0) + 1
        if blk == self.lostw_block and self.writes_seen[blk] > 1:
            return                                # aceita, mas não grava
        data = bytearray(data)
        if blk == self.corrupt_block and self.writes_seen[blk] >= self.corrupt_from:
            data[1000] ^= 0x10                    # um bit trocado
        p = pos % self.real
        self.mem[p:p + len(data)] = data

    def read(self, pos, size):
        self._tick()
        blk = pos // BLK
        if blk == self.rerr_block:
            raise OSError(errno.EIO, "Input/output error")
        p = pos % self.real
        out = bytearray(self.mem[p:p + size])
        if blk == self.unstable_block:
            out[random.randrange(size)] ^= 1 << random.randrange(8)
        return bytes(out)

    def flush(self):
        self._tick()


def run(card, **kw):
    kw.setdefault("mode", "escrita")
    kw.setdefault("passes", 2)
    evs = []
    res = Diagnoser(card, card.total, block=BLK, emit=evs.append, seed=1234, **kw).run()
    return res, evs


class DiagnoseTest(unittest.TestCase):
    def test_healthy_write(self):
        res, evs = run(FakeCard(), passes=5)
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["verdict"], "Saudável")
        self.assertGreaterEqual(res["score"], 85)
        self.assertEqual(res["written"], TOTAL * 5)
        kinds = {e["ev"] for e in evs}
        self.assertTrue({"dstart", "dphase", "dblocks", "dpass"} <= kinds)
        pats = [s["pattern"] for s in res["summaries"]]
        self.assertEqual(pats, ["aleatório", "0x55", "0xAA", "0xFF", "0x00"])

    def test_corruption(self):
        res, _ = run(FakeCard(corrupt_block=5))
        self.assertEqual(res["counts"]["corrupt"], 1)
        self.assertEqual(res["verdict"], "Reprovado")
        self.assertEqual(res["summaries"][0]["read"]["bad_sectors"], 1)

    def test_fake_capacity(self):
        res, _ = run(FakeCard(real=TOTAL // 2), passes=1)
        self.assertEqual(res["verdict"], "Falsificado")
        self.assertEqual(res["score"], 0)
        self.assertGreater(res["counts"]["alias"], 0)

    def test_lost_write(self):
        res, _ = run(FakeCard(lostw_block=7), passes=2)
        self.assertEqual(res["counts"]["lost_writes"], 1)
        self.assertLess(res["score"], 30)

    def test_read_only_unstable(self):
        card = FakeCard(unstable_block=3)
        before = bytes(card.mem)
        res, _ = run(card, mode="leitura", passes=3)
        self.assertEqual(bytes(card.mem), before)           # não alterou nada
        self.assertEqual(res["counts"]["unstable"], 1)
        self.assertEqual(res["written"], 0)

    def test_read_error(self):
        res, _ = run(FakeCard(rerr_block=2), mode="leitura", passes=1)
        self.assertEqual(res["counts"]["read_errors"], 1)
        self.assertLessEqual(res["score"], 45)

    def test_device_lost(self):
        res, _ = run(FakeCard(die_after=50), passes=3)
        self.assertEqual(res["status"], "lost")
        self.assertLessEqual(res["score"], 5)

    def test_until_fail(self):
        res, _ = run(FakeCard(corrupt_block=9, corrupt_from_pass=3), until_fail=True)
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["passes_done"], 3)


if __name__ == "__main__":
    unittest.main()
