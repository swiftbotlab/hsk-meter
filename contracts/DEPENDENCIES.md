# Vendored dependencies

Both are committed unmodified, byte-for-byte identical to the official releases below (checked with
`./verify_deps.sh`, which downloads the releases into a temporary directory and compares every file).

| Path | Package | Release | Provenance |
|---|---|---|---|
| `node_modules/@openzeppelin/contracts/` | `@openzeppelin/contracts` | 5.1.0 (npm) | tarball `https://registry.npmjs.org/@openzeppelin/contracts/-/contracts-5.1.0.tgz`, npm integrity `sha512-p1ULhl7BXzjjbha5aqst+QMLY+4/LCWADXOCsmLHRM77AqiPjnd9vvUN9sosUfhL9JGKpZ0TjEGxgvnizmWGSA==`, shasum `4e61162f2a2bf414c4e10c45eca98ce5f1aadbd4` |
| `lib/forge-std/` | `foundry-rs/forge-std` | tag `v1.16.2` | commit `bf647bd6046f2f7da30d0c2bf435e5c76a780c1b`, GitHub tag archive sha256 `35dbdf9c98db566f150a70b723f11d831feebb01ce615245a4fcaa4652fd4275` (tests only, not deployed) |

The deployed contract imports only `ReentrancyGuard`, `IERC20` and `SafeERC20` from OpenZeppelin 5.1.0.
