# Third-party software

This onboarding repository imports, but does not copy, the source in
[`KinesisCTP/hovermap_ros_api`](https://github.com/KinesisCTP/hovermap_ros_api),
which tracks [`Emesent/hovermap_ros_api`](https://github.com/Emesent/hovermap_ros_api).

At the audited commit `1608fb74784977b69936590b9cda8340a1fe3013`:

- the repository-level `LICENSE` and `REUSE.toml` identify Emesent-authored
  material as MIT-licensed;
- `src/mule_bridge/` carries the CSIRO Open Source Software Licence Agreement;
- the `hovermap_api` and `hovermap_api_msgs` package manifests nevertheless
  state `Proprietary`.

Those upstream notices and files govern the imported source. The inconsistency
must be clarified with Emesent before publishing or redistributing a derivative
port. Nothing in this repository relicenses Emesent or CSIRO material.

Device-bound feature/entitlement files and downloaded scans are not part of
this repository and must not be committed.
