trigger AccountTrigger on Account (before insert) {
    AccountService.refresh(Trigger.new[0]);
}
