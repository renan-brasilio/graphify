public with sharing class AccountService {
    @AuraEnabled(cacheable=true)
    public static List<Account> getAccounts(String pIndustry) {
        return [SELECT Id, Name FROM Account WHERE Industry = :pIndustry];
    }

    public static void refresh(Account acct) {
        AccountSelector.refreshCache();
        acct.Name = 'Updated';
    }
}
