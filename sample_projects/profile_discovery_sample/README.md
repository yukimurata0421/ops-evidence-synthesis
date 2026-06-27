# Profile Discovery Sample

Small local fixture for log-driven profile discovery.

Raw source and raw env values are not uploaded by the discovery pipeline. The
fixture includes a systemd unit, Python entry points, a dependency manifest, and
dummy environment keys so tests can verify sanitized profile mapping.
