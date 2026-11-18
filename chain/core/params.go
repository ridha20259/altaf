package core

const (
	Unit            = uint64(1_000_000)
	TotalSupply     = 21_000_000 * Unit
	MinSupply       = 10_000_000 * Unit
	MiningPool      = 2_000_000 * Unit
	BridgeGenesis   = TotalSupply - MiningPool
	Phase1Reward    = uint64(7_610_350)
	MinerCap        = 100_000 * Unit
	FounderPercent  = 10
	MinFee          = Unit / 100
	AlgoSHA         = 0
	AlgoCPU         = 1
	NumAlgos        = 2
	AlgoSpacing     = 240
	LWMAWindow      = 45
	EpochBlocks     = 21_600
	BurnMintPercent = 90
	MaxReorgDepth   = 20
	MaxBlockTxs     = 1000
	MaxFutureTime   = 15 * 60
	CoinbaseMagic   = "ALTF"
	AddrVersion     = 0x17
)

var Phase2PerBlock = [NumAlgos]uint64{10 * Unit / 360, 1 * Unit / 360}

// Easiest allowed targets (compact) and starting difficulty; LWMA adapts. Set per network by UseNetwork.
var PowLimitBits = [NumAlgos]uint32{0x1f00ffff, 0x2000ffff}
var GenesisBits = [NumAlgos]uint32{0x1e00ffff, 0x1f00ffff}

func UseNetwork(name string) {
	PowLimitBits = [NumAlgos]uint32{0x1f00ffff, 0x2000ffff}
	GenesisBits = [NumAlgos]uint32{0x1e00ffff, 0x1f00ffff}
	if name == "regtest" {
		PowLimitBits = [NumAlgos]uint32{0x207fffff, 0x207fffff}
		GenesisBits = PowLimitBits
	}
}

type Params struct {
	Founder     string
	Bridge      string
	GenesisTime uint32
	Network     string
}
