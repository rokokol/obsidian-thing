{
  description = "MCP server for local Obsidian vaults";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nix-best-practices = {
      url = "github:rokokol/nix-best-practices-skill";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      nix-best-practices,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (
        pkgs:
        let
          inherit (builtins.fromTOML (builtins.readFile ./pyproject.toml)) project;
          lockedPython = (builtins.fromTOML (builtins.readFile ./uv.lock)).package;
          lockedPackage =
            name: attrs:
            let
              package = lib.findFirst (entry: entry.name == name) null lockedPython;
            in
            pkgs.python3Packages.buildPythonPackage (
              {
                pname = name;
                inherit (package) version;
                pyproject = true;
                src = pkgs.fetchurl {
                  inherit (package.sdist) url;
                  hash = builtins.convertHash {
                    hash = lib.removePrefix "sha256:" package.sdist.hash;
                    hashAlgo = "sha256";
                    toHashFormat = "sri";
                  };
                };
              }
              // attrs
            );
          excalidrawRender = lockedPackage "excalidraw-render" {
            build-system = with pkgs.python3Packages; [ hatchling ];
            dependencies = with pkgs.python3Packages; [
              cairosvg
              pillow
            ];
            pythonImportsCheck = [ "excalidraw_render" ];
            meta = {
              description = "Export Excalidraw drawings without a browser";
              homepage = "https://github.com/shivama205/excalidraw-render";
              license = lib.licenses.mit;
              mainProgram = "excalidraw-render";
            };
          };
          lzString = lockedPackage "lzstring" {
            build-system = with pkgs.python3Packages; [ setuptools ];
            postPatch = ''
              substituteInPlace setup.py --replace-fail "install_requires=['future>=0.14.0']" 'install_requires=[]'
              substituteInPlace lzstring/__init__.py \
                --replace-fail 'from future import standard_library' "" \
                --replace-fail 'standard_library.install_aliases()' ""
            '';
            pythonImportsCheck = [ "lzstring" ];
            meta = {
              description = "Python decoder for LZ-String data";
              homepage = "https://github.com/gkovacs/lz-string-python";
              license = lib.licenses.mit;
            };
          };
          app = pkgs.python3Packages.buildPythonApplication {
            pname = project.name;
            version = lib.fileContents ./VERSION;
            pyproject = true;
            src = lib.fileset.toSource {
              root = ./.;
              fileset = lib.fileset.unions [
                ./pyproject.toml
                ./VERSION
                ./src
              ];
            };
            build-system = with pkgs.python3Packages; [ hatchling ];
            dependencies = with pkgs.python3Packages; [
              cairosvg
              croniter
              excalidrawRender
              inotify-simple
              lzString
              pathspec
              markdown-it-py
              mcp
              mdit-py-plugins
              numpy
              pillow
              pypdf
              prometheus-client
              regex
              ruamel-yaml
              starlette
              uvicorn
            ];
            makeWrapperArgs = [
              "--prefix"
              "PATH"
              ":"
              (lib.makeBinPath (with pkgs; [ ffmpeg-headless ]))
            ];
            pythonImportsCheck = [
              "obsidian_thing.server"
              "obsidian_thing.projection"
            ];
            meta = {
              description = "MCP server for local Obsidian vaults";
              mainProgram = "obsidian-thing";
              platforms = systems;
            };
          };
        in
        {
          default = app;
          docker = pkgs.dockerTools.buildLayeredImage {
            inherit (project) name;
            tag = "local";
            config = {
              Entrypoint = [ (lib.getExe app) ];
              Cmd = [ "--help" ];
              User = "65534:65534";
            };
          };
        }
      );

      nixosModules.default =
        { lib, pkgs, ... }:
        {
          imports = [ ./nixos-module.nix ];
          obsidian-thing.package = lib.mkDefault self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        };

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          LD_LIBRARY_PATH = lib.makeLibraryPath (with pkgs; [ cairo ]);
          packages = with pkgs; [
            actionlint
            ffmpeg-headless
            pyright
            python3
            uv
          ];
        };
      });

      checks = forAllSystems (pkgs: {
        nix-lint = nix-best-practices.lib.mkCheck {
          inherit pkgs;
          root = ./.;
          namespaces = [ "obsidian-thing" ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixfmt-tree);
    };
}
