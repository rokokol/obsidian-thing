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
              croniter
              inotify-simple
              pathspec
              markdown-it-py
              mcp
              mdit-py-plugins
              numpy
              pillow
              pypdf
              prometheus-client
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
