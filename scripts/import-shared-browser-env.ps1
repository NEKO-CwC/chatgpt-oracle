# Only non-secret browser configuration is shared between the two providers.
foreach ($configName in @('ORACLE_SHARED_BROWSER', 'ORACLE_BROWSER_PROFILE_DIR', 'ORACLE_BROWSER_CDP_URL', 'ORACLE_BROWSER_LAUNCH_CDP')) {
    if (-not [Environment]::GetEnvironmentVariable($configName, 'Process')) {
        $configuredValue = [Environment]::GetEnvironmentVariable($configName, 'User')
        if ($configuredValue) { [Environment]::SetEnvironmentVariable($configName, $configuredValue, 'Process') }
    }
}
