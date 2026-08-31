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

        # 3.12 is the deployment interpreter: Kolla 2025.1 builds on Ubuntu
        # Noble 24.04. CI keeps a 3.10 leg for the api_client suite only.
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
            # cursive, so that suite installs from PyPI into a venv,
            # pinned by uv.lock. Building it with nix instead means
            # adopting uv2nix, which the lock now makes possible (#23).
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
            echo "    python3 -m flake8 truenas_cinder_driver tests"
            echo "    python3 -m pytest tests/functional          # needs a dev appliance"
            echo ""
            echo "  Driver tests need Cinder, which is not in nixpkgs:"
            echo "    uv venv && uv pip install -e '.[driver]'"
            echo "    .venv/bin/python -m pytest tests/driver"
            echo "----------------------------------------------------"
          '';
        };
      });
}
