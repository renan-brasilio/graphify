public with sharing class AccountService {
    public static void refresh(Account acct) {
        acct.Name = 'Updated';
    }
}
