# ALTAF chain (native PoW) — design
Account model, 6 decimals (1 ALTF = 1_000_000 units), ed25519 keys, address = base58check(0x17 || sha256(pub)[:20]) (starts with "A").
Header 80 bytes Bitcoin layout: version(LE u32, algo = version & 0xff: 0=SHA256d ASIC, 1=Argon2id CPU) prev(32) merkle(32) time(u32) bits(u32 compact) nonce(u32).
Merkle: Bitcoin style sha256d; leaf0 = sha256d(coinbase bytes); coinbase = "ALTF"|height u64LE|miner addr 21B|extranonce 8B|tag. Stratum-compatible (coinb1 = up to addr, coinb2 = tag).
PoW: SHA = sha256d(header); CPU = argon2id(header, salt "ALTAF-CPU-POW-v1", t=1, m=32MiB, p=1). Hash read little-endian ≤ target.
Difficulty: per-algo LWMA(N=45), spacing 240 s per algo (≈120 s combined).
Genesis: pool 2,000,000 (consensus mining pool) + bridge account 19,000,000 = 21,000,000.
Phase 1: reward 7.610350/block from pool until pool empty (~12 months). 10 % founder, 90 % miner; miner capped 100,000 ALTF per address over phase 1 (excess stays in pool).
Phase 2: per block CPU 1 ALTF/day/360, SHA 10 ALTF/day/360, limited by epoch budget = 90 % of burn in previous epoch (21,600 blocks). 10 % founder.
Fees: min 0.01 ALTF; half to miner, half burned; burn stops when supply ≤ 10,000,000 (then all to miner). Supply never > 21,000,000.
Fork choice: most blocks-weight; max reorg depth 20; checkpoints signed by seed node later.
