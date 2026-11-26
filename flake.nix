{
  description = "MCP server for local Obsidian vaults";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
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
          project = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project;
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
            build-system = [ pkgs.python3Packages.hatchling ];
            dependencies = [
              pkgs.python3Packages.inotify-simple
              pkgs.python3Packages.pathspec
              pkgs.python3Packages.markdown-it-py
              pkgs.python3Packages.mcp
              pkgs.python3Packages.mdit-py-plugins
              pkgs.python3Packages.ruamel-yaml
              pkgs.python3Packages.starlette
              pkgs.python3Packages.uvicorn
            ];
            pythonImportsCheck = [ "obsidian_thing.server" ];
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
            name = project.name;
            tag = "local";
            config = {
              Entrypoint = [ (lib.getExe app) ];
              Cmd = [ "--help" ];
              User = "65534:65534";
            };
          };
        }
      );

      formatter = forAllSystems (pkgs: pkgs.nixfmt-tree);
    };
}
