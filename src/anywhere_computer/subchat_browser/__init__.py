"""Optional ordinary Chat browser adapter; requires the browser extra."""

# A login made in ordinary Chrome uses the macOS keychain. Playwright's mock
# keychain would make that profile's saved cookies unreadable on the next launch.
CHROME_PROFILE_IGNORED_DEFAULT_ARGS = ('--use-mock-keychain',)
