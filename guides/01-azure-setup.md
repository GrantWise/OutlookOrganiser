# Step 1: Azure AD App Registration

> **Time required:** ~10 minutes
> **Previous:** [Prerequisites](README.md) | **Next:** [Installation](02-installation.md)

## What This Step Does

The assistant needs permission to read and organize your email. Microsoft requires you to register an "app" in Azure Portal before any software can access your mailbox. This is not installing software -- it is creating a permission record that says "this application is allowed to access my email."

Registering an app is free. You do not need a paid Azure subscription.

## Before You Start

- You need the Microsoft account that has the email you want to organize (work, school, or personal Outlook.com)
- If your organization restricts app registrations, you may need help from your IT administrator. You will know this is the case if you see a "You do not have permission" message during registration. See [Troubleshooting](05-troubleshooting.md#i-cannot-register-an-app-it-says-you-do-not-have-permission) if this happens.

## Step-by-Step Walkthrough

### 1.1 Open Azure Portal

Go to [https://portal.azure.com](https://portal.azure.com) in your web browser.

Sign in with the same Microsoft account that has the email you want to organize.

After signing in, you should see the Azure Portal home page. There is a search bar at the top and a grid of service icons below it. You may also see a "Welcome to Azure" banner if this is your first visit -- you can dismiss it.

### 1.2 Navigate to Microsoft Entra ID

Click in the **search bar** at the top of the page and type **Microsoft Entra ID**. Select it from the dropdown results.

You should now see the Microsoft Entra ID overview page. The left sidebar shows options including Overview, Users, Groups, and App registrations.

> **Note:** Microsoft Entra ID is the new name for what used to be called "Azure Active Directory" or "Azure AD." If you see the old name referenced in other documentation, it is the same service.

### 1.3 Open App Registrations

In the left sidebar, expand the **Manage** section and click **App registrations**.

You should see a page titled "App registrations" with tabs for "Owned applications" and "All applications." If you have never registered an app before, the list will be empty.

### 1.4 Create a New Registration

Click the **+ New registration** button near the top of the page.

A form appears with three fields. Fill them in as follows:

**Name:**
Enter `Outlook AI Assistant` (or any name you like -- this is just a label for your reference).

**Supported account types:**
This determines which Microsoft accounts can use the app. Choose the option that matches your situation:

- **"Accounts in this organizational directory only"** -- Choose this if you have a work or school account and only want to use the assistant with that account. This is the most common choice for corporate email.

- **"Accounts in any organizational directory and personal Microsoft accounts"** -- Choose this if you have a personal Outlook.com / Hotmail account, or if you want flexibility to use it with different account types.

If you are unsure, select the second option. It is more permissive but works for all account types.

**Redirect URI:**
Leave this completely blank. The assistant uses device code flow authentication, which does not need a redirect URI.

Click **Register**.

### 1.5 Copy Your Application IDs

After clicking Register, you are taken to the app's **Overview** page. You should see the app name you entered at the top, and below it two important values:

- **Application (client) ID** -- a long string of letters, numbers, and dashes (a UUID), for example: `a1b2c3d4-e5f6-7890-abcd-ef1234567890`
- **Directory (tenant) ID** -- another UUID in the same format

Click the **copy icon** next to each value and save them somewhere temporarily (a notepad, text file, or note on your phone). You will need both of these in the next guide when you configure the assistant.

> **Personal Outlook.com accounts:** If you are using a personal Microsoft account (not a work/school account), you will use the word `common` instead of your tenant ID when configuring the assistant. You can still copy the tenant ID here for reference, but you will enter `common` in the config file later.

### 1.6 Enable Device Code Flow

The assistant authenticates by showing you a code in your terminal, which you then enter in a browser. This authentication method is called "device code flow" and it must be explicitly enabled.

1. In the left sidebar of your app's page (where you copied the IDs), expand **Manage** and click **Authentication**
2. Scroll down to the **Advanced settings** section at the bottom of the page
3. Find **Allow public client flows** and set it to **Yes**
4. Click **Save** at the top of the page

You should see a green notification banner confirming the settings were saved.

> **Why this matters:** Without this setting, the assistant cannot generate the login code you need to authenticate. If you skip this step, you will see an error like "AADSTS7000218" when you try to run the assistant.

### 1.7 Add API Permissions

Now you need to grant the assistant permission to access specific parts of your mailbox.

1. In the left sidebar, click **API permissions**
2. You should see one permission already listed: **User.Read** under Microsoft Graph. This was added automatically.
3. Click **+ Add a permission**
4. In the panel that appears on the right, click **Microsoft Graph**
5. Click **Delegated permissions** (not Application permissions)

Now you need to find and select three additional permissions. Use the search box at the top of the permission list to find each one:

| Permission | What to search for | What it allows |
|---|---|---|
| **Mail.ReadWrite** | Type `Mail.ReadWrite` | Lets the assistant read your emails and move them between folders |
| **Mail.Send** | Type `Mail.Send` | Lets the assistant send you a daily digest summary email |
| **MailboxSettings.Read** | Type `MailboxSettings` | Lets the assistant read your timezone for correct scheduling |

For each permission:
1. Type the name in the search box
2. Expand the section if needed (click the arrow next to the permission name)
3. Check the box next to the permission
4. After selecting all three, click **Add permissions** at the bottom

You should now see four permissions listed on the API permissions page:
- Mail.ReadWrite
- Mail.Send
- MailboxSettings.Read
- User.Read

### 1.8 Grant Admin Consent (If Available)

Look for a button at the top of the API permissions page that says **Grant admin consent for [your organization name]**.

- **If you see this button:** Click it, then click **Yes** to confirm. Green checkmarks should appear in the "Status" column next to each permission.
- **If you do not see this button:** That is normal for personal accounts. Consent will be granted automatically the first time you sign in through the assistant.
- **If the button is grayed out:** Your organization requires an admin to grant consent. Ask your IT administrator to grant consent for this app, or see [Troubleshooting](05-troubleshooting.md#user-or-admin-has-not-consented).

### 1.9 Verify Your Setup

Before moving on, confirm everything is in place:

- [ ] You are on the **API permissions** page and see four permissions listed
- [ ] Either green checkmarks appear next to each permission, or you have a personal account (consent will happen on first sign-in)
- [ ] You have your **Application (client) ID** saved somewhere
- [ ] You have your **Directory (tenant) ID** saved (or you know to use `common` for personal accounts)
- [ ] **Allow public client flows** is set to **Yes** (check under Authentication > Advanced settings)

## What You Have Now

You have registered an app in Azure that gives the assistant permission to:
- Read and move your emails (Mail.ReadWrite)
- Send you digest summaries (Mail.Send)
- Check your timezone (MailboxSettings.Read)
- Identify your email address (User.Read)

Keep your Application (client) ID and Directory (tenant) ID handy -- you will enter them in the configuration file in the next step.

## If Something Went Wrong

See [Troubleshooting > Azure AD Issues](05-troubleshooting.md#azure-ad-issues) for help with common registration problems.

---

> **Next:** [Installation](02-installation.md)
