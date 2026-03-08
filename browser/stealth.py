from playwright.async_api import Page

STEALTH_SCRIPTS = [
    # Hide webdriver flag
    """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    """,
    # Fake plugins
    """
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5]
    });
    """,
    # Fake languages
    """
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en']
    });
    """,
    # Chrome runtime
    """
    window.chrome = { runtime: {} };
    """,
    # Permissions
    """
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);
    """,
]


async def apply_stealth(page: Page) -> None:
    for script in STEALTH_SCRIPTS:
        await page.add_init_script(script)
