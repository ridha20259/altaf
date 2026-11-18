package core

// Founder = Ridha's native address (10% of every block). Bridge = custody of the 19,000,000
// genesis coins that mirror TRC20 ALTF (released only against locked TRC20 tokens).
// Mainnet addresses are set at launch; testnet/regtest use test keys.
var Networks = map[string]Params{
	"mainnet": {Founder: "AVbeUk7UasFiuj3V89pVPRWi3r39ohxATL", Bridge: "AGrwvb3SwnhPfYXaH6rMiSB211VwHsJaLW", GenesisTime: 1794928469, Network: "mainnet"},
	"testnet": {Founder: "Ab9U1aS6cxseVerVbDF7r915iT85umMZZL", Bridge: "AMwtRVCUyFtYy3sRahyrtwp6kYZWXrFFzk", GenesisTime: 1790000000, Network: "testnet"},
	"regtest": {Founder: "Ab9U1aS6cxseVerVbDF7r915iT85umMZZL", Bridge: "AMwtRVCUyFtYy3sRahyrtwp6kYZWXrFFzk", GenesisTime: 1790000000, Network: "regtest"},
}
