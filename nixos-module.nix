{
  config,
  lib,
  pkgs,
  utils,
  ...
}:

let
  cfg = config.obsidian-thing;
  toml = pkgs.formats.toml { };
  serviceName = "obsidian-thing";
  absolutePath = lib.types.strMatching "/.*";
  vaults = cfg.settings.vaults or { };
  vaultNames = builtins.attrNames vaults;
  needsState =
    vault:
    (vault.indexing.auto or false)
    || (vault ? embeddings && ((vault.embeddings.enabled or true) || vault.embeddings ? state_dir));
  stateVaults = lib.filterAttrs (_: needsState) vaults;
  hasExternalState = vault: (vault.embeddings or { }) ? state_dir;
  provisioned = lib.filterAttrs (_: vault: !hasExternalState vault) stateVaults;
  stateEntry = name: "${serviceName}/${name}";
  builtSettings = cfg.settings // {
    vaults = lib.mapAttrs (
      name: vault:
      if builtins.hasAttr name provisioned then
        vault
        // {
          embeddings = (vault.embeddings or { enabled = false; }) // {
            state_dir = "/var/lib/${stateEntry name}";
          };
        }
      else
        vault
    ) vaults;
  };
  configFile = toml.generate "obsidian-thing.toml" builtSettings;
  writableVaultPaths = lib.concatMap (
    vault:
    lib.optionals (vault.writable or false) (
      [ vault.root ] ++ lib.optional (vault ? trash_dir) vault.trash_dir
    )
  ) (builtins.attrValues vaults);
  externalStatePaths = lib.mapAttrsToList (_: vault: vault.embeddings.state_dir) (
    lib.filterAttrs (_: hasExternalState) stateVaults
  );
  isAbsolute = value: builtins.isString value && lib.hasPrefix "/" value;
in
{
  options.obsidian-thing = {
    enable = lib.mkEnableOption "the Obsidian Thing MCP service";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The Obsidian Thing package.";
    };

    settings = lib.mkOption {
      inherit (toml) type;
      default = { };
      description = "Non-secret settings in the same form as the application TOML file.";
    };

    environmentFile = lib.mkOption {
      type = absolutePath;
      description = "Path to a runtime environment file with the bearer tokens and provider credentials.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "The address the service listens on.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8765;
      description = "The TCP port the service listens on.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = serviceName;
      description = "The system user with access to the configured vaults.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = serviceName;
      description = "The primary group of the service process.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion =
          vaultNames != [ ] && lib.all (name: builtins.match "[a-zA-Z0-9_-]+" name != null) vaultNames;
        message = "Configure obsidian-thing.settings.vaults with at least one valid vault ID.";
      }
      {
        assertion = lib.all (vault: isAbsolute (vault.root or null)) (builtins.attrValues vaults);
        message = "Set an absolute root for each Obsidian Thing vault.";
      }
      {
        assertion = lib.all isAbsolute (externalStatePaths ++ writableVaultPaths);
        message = "Set absolute paths for Obsidian Thing state and writable directories.";
      }
      {
        assertion = cfg.user != "root" && cfg.group != "root";
        message = "Run Obsidian Thing with an unprivileged user and group.";
      }
    ];

    users = {
      groups = lib.mkIf (cfg.group == serviceName) { ${serviceName} = { }; };
      users = lib.mkIf (cfg.user == serviceName) {
        ${serviceName} = {
          isSystemUser = true;
          inherit (cfg) group;
        };
      };
    };

    systemd.services.${serviceName} = {
      description = "Obsidian Thing MCP server";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      serviceConfig = {
        User = cfg.user;
        Group = cfg.group;
        EnvironmentFile = cfg.environmentFile;
        ExecStart = utils.escapeSystemdExecArgs [
          (lib.getExe cfg.package)
          "--config"
          configFile
          "--host"
          cfg.host
          "--port"
          (toString cfg.port)
        ];
        StateDirectory = lib.optionals (provisioned != { }) (
          [ serviceName ] ++ map stateEntry (builtins.attrNames provisioned)
        );
        StateDirectoryMode = "0700";
        UMask = "0077";
        ReadWritePaths = externalStatePaths ++ writableVaultPaths;
        ProtectSystem = "strict";
        ProtectHome = "read-only";
        PrivateTmp = true;
        NoNewPrivileges = true;
        Restart = "on-failure";
      };
    };
  };
}
