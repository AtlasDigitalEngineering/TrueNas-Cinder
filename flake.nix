{
  description = "TrueNAS Cinder driver development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        # 3.12, not the 3.10 deployment target: python310 is no longer in
        # nixpkgs, and Cinder 26.x (OpenStack 2025.1) runs fine on 3.12.
        # CI still tests 3.10 via setup-python, which is what Kolla ships.
        python = pkgs.python312;

        pythonEnv = python.withPackages (ps: with ps; [
          requests
          pytest
          pytest-cov
          coverage
          flake8
          tox
        ]);

        libPath = pkgs.lib.makeLibraryPath [
          pkgs.stdenv.cc.cc.lib
          pkgs.zlib
        ];
      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = [
            pythonEnv

            # For tests/driver only. Cinder is not in nixpkgs, and neither
            # are os-brick, oslo-versionedobjects, taskflow, castellan or
            # cursive, so that suite installs from PyPI into a venv. Making
            # it reproducible too means packaging Cinder's whole tree, or
            # adopting uv2nix once #23 adds a pyproject.toml -- the latter
            # is the better path and belongs to that issue.
            pkgs.uv

            pkgs.gh
            pkgs.git
            pkgs.curl
          ];

          shellHook = ''
            export LD_LIBRARY_PATH="${libPath}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

            # uv must not fetch its own interpreter: downloaded CPython
            # builds expect an FHS dynamic linker and will not run here, so
            # `uv venv` uses the python3 from this shell instead.
            #
            # UV_PYTHON is deliberately NOT exported. It takes precedence
            # over uv's discovery of ./.venv, so `uv pip install` would
            # target the read-only store interpreter and fail with
            # "tries to modify the immutable /nix/store".
            export UV_PYTHON_DOWNLOADS=never

            echo "--- TrueNAS Cinder Driver Development Environment ---"
            echo "  Python $(python3 --version | cut -d' ' -f2) (nix)  |  uv $(uv --version | cut -d' ' -f2)  |  $(gh --version | head -1)"
            echo ""
            echo "  Ready now, no setup (built by nix, pinned by flake.lock):"
            echo "    python3 -m pytest tests/unit"
            echo "    python3 -m flake8 truenas_cinder_driver tests tools"
            echo "    python3 tools/verify_endpoints.py [--write]   # dev appliance only"
            echo ""
            echo "  Driver tests need Cinder, which is not in nixpkgs:"
            echo "    uv venv && uv pip install -r driver-test-requirements.txt"
            echo "    .venv/bin/python -m pytest tests/driver"
            echo "----------------------------------------------------"
          '';
        };
      });
}
