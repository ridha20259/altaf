// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title Altaf (ALTF) — fixed supply 21,000,000, no mint.
/// 5,000,000 to the founder (free). 16,000,000 stay in this contract as the server reserve.
/// Each partner server that is running receives 1,000,000 locked ALTF; a locked server can move
/// at most 100 ALTF of its allocation every 90 days. When a server stops, the remaining locked
/// allocation returns to the reserve for the next server. Allocation/revocation is done by the
/// operator (the ALTAF network monitor) or the owner; only the owner can change the operator.
contract Altaf {
    string public constant name = "Altaf";
    string public constant symbol = "ALTF";
    uint8 public constant decimals = 6;
    uint256 public constant totalSupply = 21_000_000 * 10**6;
    uint256 public constant SERVER_ALLOCATION = 1_000_000 * 10**6;
    uint256 public constant MAX_SERVERS = 16;
    uint256 public constant UNLOCK_PER_PERIOD = 100 * 10**6;
    uint256 public constant PERIOD = 90 days;

    address public owner;
    address public operator;
    uint256 public activeServers;
    uint256 public burned;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    struct Server { bool active; uint256 locked; uint256 periodStart; uint256 usedInPeriod; uint256 since; }
    mapping(address => Server) public servers;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    event ServerOpened(address indexed server, uint256 amount);
    event ServerClosed(address indexed server, uint256 returned);
    event OperatorChanged(address indexed operator);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    modifier onlyOwner() { require(msg.sender == owner, "not owner"); _; }
    modifier onlyOperator() { require(msg.sender == operator || msg.sender == owner, "not operator"); _; }

    constructor(address founder, address operator_) {
        require(founder != address(0), "founder");
        owner = founder;
        operator = operator_;
        balanceOf[founder] = 5_000_000 * 10**6;
        balanceOf[address(this)] = 16_000_000 * 10**6;
        emit Transfer(address(0), founder, 5_000_000 * 10**6);
        emit Transfer(address(0), address(this), 16_000_000 * 10**6);
        emit OwnershipTransferred(address(0), founder);
        emit OperatorChanged(operator_);
    }

    /// Amount of `a` that can move right now.
    function transferable(address a) public view returns (uint256) {
        Server storage s = servers[a];
        uint256 b = balanceOf[a];
        if (s.locked == 0) return b;
        uint256 free = b > s.locked ? b - s.locked : 0;
        uint256 used = block.timestamp >= s.periodStart + PERIOD ? 0 : s.usedInPeriod;
        uint256 quota = UNLOCK_PER_PERIOD > used ? UNLOCK_PER_PERIOD - used : 0;
        if (quota > s.locked) quota = s.locked;
        return free + quota;
    }

    function _spendLocked(address from, uint256 value) internal {
        Server storage s = servers[from];
        if (s.locked == 0) return;
        uint256 b = balanceOf[from];
        uint256 free = b > s.locked ? b - s.locked : 0;
        if (value <= free) return;
        uint256 need = value - free;
        if (block.timestamp >= s.periodStart + PERIOD) { s.periodStart = block.timestamp; s.usedInPeriod = 0; }
        require(s.usedInPeriod + need <= UNLOCK_PER_PERIOD, "server coins locked: 100 ALTF per 90 days");
        s.usedInPeriod += need;
        s.locked -= need;
    }

    function _transfer(address from, address to, uint256 value) internal {
        require(to != address(0), "zero address");
        uint256 b = balanceOf[from];
        require(b >= value, "balance");
        if (from != address(this)) _spendLocked(from, value);
        unchecked { balanceOf[from] = b - value; }
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }

    function transfer(address to, uint256 value) external returns (bool) { _transfer(msg.sender, to, value); return true; }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 a = allowance[from][msg.sender];
        require(a >= value, "allowance");
        if (a != type(uint256).max) allowance[from][msg.sender] = a - value;
        _transfer(from, to, value);
        return true;
    }

    function burn(uint256 value) external {
        require(transferable(msg.sender) >= value, "balance");
        _spendLocked(msg.sender, value);
        balanceOf[msg.sender] -= value;
        burned += value;
        emit Transfer(msg.sender, address(0), value);
    }

    function reserve() external view returns (uint256) { return balanceOf[address(this)]; }

    function circulatingSupply() external view returns (uint256) { return totalSupply - balanceOf[address(this)] - burned; }

    function openServer(address server) external onlyOperator {
        require(server != address(0) && server != address(this) && server != owner, "server");
        require(!servers[server].active, "already active");
        require(activeServers < MAX_SERVERS, "all 16 servers active");
        uint256 amt = balanceOf[address(this)] < SERVER_ALLOCATION ? balanceOf[address(this)] : SERVER_ALLOCATION;
        require(amt > 0, "reserve empty");
        activeServers += 1;
        Server storage s = servers[server];
        s.active = true; s.since = block.timestamp; s.periodStart = block.timestamp; s.usedInPeriod = 0;
        _transfer(address(this), server, amt);
        s.locked += amt;
        emit ServerOpened(server, amt);
    }

    function closeServer(address server) external onlyOperator {
        Server storage s = servers[server];
        require(s.active, "not active");
        uint256 amt = s.locked < balanceOf[server] ? s.locked : balanceOf[server];
        s.active = false; s.locked = 0; s.usedInPeriod = 0;
        activeServers -= 1;
        balanceOf[server] -= amt;
        balanceOf[address(this)] += amt;
        emit Transfer(server, address(this), amt);
        emit ServerClosed(server, amt);
    }

    function setOperator(address op) external onlyOwner { operator = op; emit OperatorChanged(op); }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "zero address");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }
}
