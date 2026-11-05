# Modbus register map

This is the contract between the simulator and the HMI. Both load it from
[`config/registers.yaml`](../config/registers.yaml), and `tests/test_docs.py` fails if the tables
below stop matching that file.

- **Transport:** Modbus TCP, default port 5020 (502 needs elevated rights on most systems)
- **Unit id:** 1
- **Addresses:** 0-based protocol addresses as sent on the wire, not 3xxxx or 4xxxx display numbers
- **Numbers:** registers are unsigned 16-bit, so decimals are fixed point: `raw = engineering value × scale`
- **Out of range:** values saturate at the end of the range, as a real sensor does; a wrapped 16-bit value would show a hot bearing as cold

## Input registers (function code 04, read only)

| Point | Address | Scale | Unit | Range | Description |
|---|---|---|---|---|---|
| `belt_speed` | 0 | 100 | m/s | 0 to 8 | Measured belt surface speed |
| `motor_speed` | 1 | 100 | m/s | 0 to 8 | Drive pulley surface speed; differs from `belt_speed` when the belt slips |
| `motor_current` | 2 | 10 | A | 0 to 600 | Drive motor current |
| `bearing_temp` | 3 | 10 | degC | 0 to 200 | Drive-end bearing temperature |
| `vibration` | 4 | 100 | mm/s | 0 to 50 | Bearing vibration velocity (RMS) |
| `material_flow` | 5 | 1 | t/h | 0 to 5000 | Material flow on the belt |
| `run_state` | 6 | 1 | | 0 to 4 | Conveyor state machine (values below) |
| `fault_code` | 7 | 1 | | 0 to 15 | Active faults as a bit field (bits below) |

Example: `bearing_temp` reading raw 615 means 61.5 degC.

### `run_state` values

| Value | Meaning |
|---|---|
| 0 | `stopped` |
| 1 | `starting` |
| 2 | `running` |
| 3 | `stopping` |
| 4 | `emergency_stop` |

### `fault_code` bits

| Bit value | Meaning |
|---|---|
| 1 | `overload` |
| 2 | `bearing_overheat` |
| 4 | `belt_slip` |
| 8 | `emergency_stop` |

Example: 12 means belt slip and emergency stop together. The bits report the plant's own status. The
HMI's process alarms do not read the first three bits; they work from the measurements, as they
would have to on real equipment. Only `emergency_stop` is read from this register.

## Holding register (function codes 03 and 06, read and write)

| Point | Address | Scale | Unit | Range | Description |
|---|---|---|---|---|---|
| `speed_setpoint` | 0 | 100 | m/s | 0 to 6 | Requested belt speed |

An over-range request is clamped, and the register is rewritten with the value the plant accepted, so
reading it back always shows the real setpoint.

## Coils (function codes 01, 05 and 15, read and write)

| Point | Address | Kind | Description |
|---|---|---|---|
| `cmd_start` | 0 | Pulse | Start the conveyor |
| `cmd_stop` | 1 | Pulse | Controlled stop |
| `cmd_estop` | 2 | Pulse | Emergency stop |
| `cmd_reset` | 3 | Pulse | Reset after an emergency stop |
| `inject_overload` | 4 | Level | Fault injection: material flow surge |
| `inject_bearing_overheat` | 5 | Level | Fault injection: bearing cooling degraded |
| `inject_belt_slip` | 6 | Level | Fault injection: belt slips on the drive pulley |

**Pulse** coils are written True by the client, acted on once in the next scan, then cleared by the
simulator. A refused command is cleared too, so a start refused during an emergency stop cannot fire
after the reset.

**Level** coils stay as written, and the fault stays active for as long as the coil is on.

When several commands arrive in one scan, they run in the order reset, start, stop, emergency stop, so
the safer command has the last word.

## Known quirks

pymodbus stores coils in 16-bit words, so coil addresses 7 to 15 can be read and written but do nothing.
It also requires a non-empty discrete input table, so a single unused discrete input (function code 02)
exists. Neither is part of the contract.
