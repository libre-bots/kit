kit-error-hub = I can't reach my data at the moment. Try again in a minute.
    .title = Not reachable right now
kit-error-forbidden = I'm missing a permission for this. Ask a server admin to check my role.
    .title = Missing permission
kit-error-unknown = Try again later. If it keeps happening, give the bot's operator this reference: `{ $reference }`
    .title = Something went wrong

kit-cmd-info =
    .name = info
    .description = Shows what this bot is, who runs it and where its code is
kit-info-meta = Version { $version } · { $servers ->
        [one] { $servers } server
       *[other] { $servers } servers
    } · Run by { $operator } ({ $contact })
kit-info-meta-without-servers = Version { $version } · Run by { $operator } ({ $contact })
kit-info-privacy = Use /privacy to see, export or delete what this bot stores about you.
kit-info-source = Source code
kit-info-support = Support
kit-info-privacy-policy = Privacy policy

kit-cmd-privacy =
    .name = privacy
    .description = See, export or delete what this bot stores about you
kit-privacy-overview = { $operator } ({ $contact }) runs this instance of { $bot }.
    .title = Your data
    .stored = It stores about you:
    .nothing = It stores nothing about you.
    .policy = Privacy policy
kit-privacy-export = This file holds everything { $bot } stores about you.
    .title = Your data export
    .button = Export my data
kit-privacy-delete = This deletes everything { $bot } stores about you, in every server:
    .title = Delete everything about you?
    .warning = It cannot be undone. Export your data first if you want a copy.
    .button = Delete my data
    .confirm = Delete everything
    .modal = Delete all your data
    .input = Type your username to confirm
    .mismatch = That is not your username, so nothing was deleted.
kit-privacy-deleting = Your data is being deleted in the background. This can take a few minutes.
    .title = Deletion started
kit-privacy-server = This deletes everything { $bot } stores about this server:
    .title = Delete this server's data?
    .warning = It cannot be undone. The bot's messages in this server stay.
    .button = Delete this server's data
    .confirm = Delete server data
    .modal = Delete this server's data
    .input = Type the server name to confirm
    .mismatch = That is not the server's name, so nothing was deleted.
    .owner-only = Only the server owner can delete this server's data.
kit-privacy-server-deleted = { $bot } deleted:
    .title = Server data deleted
    .nothing = { $bot } stored nothing about this server.
kit-privacy-unconfirmed = I could not reach my data to finish this, so I cannot tell whether it was deleted. Run /privacy again in a minute to check.
    .title = Deletion not confirmed
kit-privacy-not-deleted =
    .title = Nothing was deleted
kit-privacy-cancel = Cancel
