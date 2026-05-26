@isTest
private class AccountServiceTest {
    @TestSetup
    static void setupData() {
    }

    @isTest
    static void testRefresh() {
        AccountService.refresh(null);
    }
}
