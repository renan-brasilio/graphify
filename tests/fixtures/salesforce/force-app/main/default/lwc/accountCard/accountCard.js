import { LightningElement } from 'lwc';
import getAccount from '@salesforce/apex/AccountService.refresh';
import INDUSTRY_FIELD from '@salesforce/schema/Account.Industry';
import helper from 'c/utils';

export default class AccountCard extends LightningElement {}
