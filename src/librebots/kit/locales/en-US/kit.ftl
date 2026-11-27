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
