
from __future__ import with_statement

import web
from utils import forms, helpers, auth
from settings import db, render, session
from utils.auth import require_login
import config

from datetime import datetime

urls = (
  '', 'redir',
  '/', 'index',
  '/new', 'new',
  '/login', 'login',
  '/signup', 'signup',
  '/verify', 'checkID',
  '/(.*)/share', 'share',
  '/(.*)/signatories', 'signatories',
  '/(.*)', 'petition'
)

render_plain = web.template.render('templates/') #without base, useful for sending mails

class redir:
    def GET(self): raise web.seeother('/')

class checkID:
    def POST(self):
        "Return True if petition with id `pid` does not exist"
        pid = web.input().pid
        exists = bool(db.select('petition', where='id=$pid', vars=locals()))
        return pid != 'new' and not(exists)

class index:
    def GET(self):
        petitions = db.select(['petition', 'signatory'],
                    what='petition.id, petition.title, count(signatory.user_id) as signature_count',
                    where='petition.id = signatory.petition_id and petition.deleted is null',
                    group='petition.id, petition.title',
                    order='count(signatory.user_id) desc'
                    )

        msg, msg_type = helpers.get_delete_msg()
        return render.petition_list(petitions, msg)

def fill_user_details(form, fillings=['email', 'name', 'contact']):
    details = {}
    email = helpers.get_loggedin_email() or helpers.get_unverified_email()
    if email:
        if 'email' in fillings:
            details['email'] = email

        user = db.select('users', where='email=$email', vars=locals())
        if user:
            user = user[0]
            if 'name' in fillings:
                details['userid'] = user.id
                details['prefix'] = user.prefix
                details['fname'] = user.fname
                details['lname'] = user.lname
            if 'contact' in fillings:
                details['prefix'] = user.prefix
                details['addr1'] = user.addr1
                details['addr2'] = user.addr2
                details['city'] = user.city
                details['zipcode'] = user.zip5
                details['zip4'] = user.zip4
                details['phone'] = user.phone
                
        form.fill(**details)

def send_to_congress(i, pform, wyrform, sign_id):
    from webapp import write_your_rep
    wyr = write_your_rep()
    wyr.set_dist(i)
    wyr.set_msg_id(sign_id, petition=True)
    return wyr.send_msg(i, wyrform, pform)

def create_petition(i, email):
    tocongress = i.get('tocongress', 'off') == 'on'
    i.pid = i.pid.replace(' ', '_')
    u = helpers.get_user_by_email(email)
    msg_sent = (tocongress and 'D') or 'N' # D=sending due; N=not for congress  
    with db.transaction():
        db.insert('petition', seqname=False, id=i.pid, title=i.ptitle, description=i.msg,
                    owner_id=u.id, to_congress=tocongress)
        sign_id = db.insert('signatory', user_id=u.id, share_with='A', petition_id=i.pid, sent_to_congress=msg_sent)
        
    if tocongress:
        pform, wyrform = forms.petitionform(), forms.wyrform()
        pform.fill(i), wyrform.fill(i)
        sent_status = send_to_congress(i, pform, wyrform, sign_id)
        if not isinstance(sent_status, bool): #in case that redirects with a captcha
            return sent_status
        if sent_status:
            db.update('signatory', where='id=$sign_id', sent_to_congress='S', vars=locals())    
        
    msg = """Congratulations, you've created your petition.
             Now sign and share it with all your friends."""
    helpers.set_msg(msg)

class new:
    def GET(self):
        pform = forms.petitionform()
        cform = forms.wyrform()
        fill_user_details(pform)
        email = helpers.get_loggedin_email() or helpers.get_unverified_email()
        return render.petitionform(pform, cform)

    def POST(self, input=None):
        from utils.writerep import get_wyrform
        i = input or web.input()
        tocongress = i.get('tocongress', 'off') == 'on'
        pform = forms.petitionform()
        wyrform = get_wyrform(i)
        wyr_valid = (not(tocongress) or wyrform.validates(i))
        if not pform.validates(i) or not wyr_valid:
            return render.petitionform(pform, wyrform)

        email = helpers.get_loggedin_email()
        if not email:
            return login().GET(i)

        create_petition(i, email)
        raise web.seeother('/%s' % i.pid)

class login:
    def GET(self, i):
        lf, sf = forms.loginform(), forms.signupform()
        pf, wf = forms.petitionform(), forms.wyrform()
        pf.fill(i), wf.fill(i)
        return render.petitionlogin(lf, sf, pf, wf)

    def POST(self):
        i = web.input()
        lf, pf, wf = forms.loginform(), forms.petitionform(), forms.wyrform()
        if not lf.validates(i):
            sf, wf = forms.signupform(), forms.wyrform()
            lf.fill(i), pf.fill(i), wf.fill(i)
            return render.petitionlogin(lf, sf, pf, wf)
        create_petition(i, i.useremail)
        raise web.seeother('/%s' % i.pid)

class signup:
    def POST(self):
        i = web.input()
        sf = forms.signupform()
        if not sf.validates(i):
            lf, pf, wf = forms.loginform(), forms.petitionform(), forms.wyrform()
            sf.fill(i), pf.fill(i), wf.fill(i)
            return render.petitionlogin(lf, sf, pf, wf)
        user = auth.new_user( i.fname, i.lname, i.email, i.password)
        helpers.set_login_cookie(i.email)
        create_petition(i, i.email)
        raise web.seeother('/%s' % i.pid)

def askforpasswd(user_id):
    useremail = helpers.get_loggedin_email()
    #if the current user is the owner of the petition and has not set the password
    r = db.select('users', where='id=$user_id AND email=$useremail AND password is NULL', vars=locals())
    return bool(r)

def save_password(forminput):
    password = auth.encrypt_password(forminput.password)
    db.update('users', where='id=$forminput.user_id', password=password, vars=locals())
    helpers.set_msg('Password stored')

def save_signature(forminput, pid):
    try:
        user = db.select('users', where='email=$forminput.email', vars=locals())[0]
    except:
        user_id = db.insert('users', lname=forminput.lname, fname=forminput.fname, email=forminput.email)
        user = web.storage(id=user_id, lname=forminput.lname, fname=forminput.fname, email=forminput.email)

    signed = db.select('signatory', where='petition_id=$pid AND user_id=$user.id', vars=locals())
    share_with = (forminput.get('share_with', 'off') == 'on' and 'N') or 'A'
    if not signed:
        signature = dict(petition_id=pid, user_id=user.id,
                        share_with=share_with, comment=forminput.comment)
        db.insert('signatory', **signature)
        helpers.set_msg('Thanks for your signing! Why don\'t you tell your friends about it now?')
        helpers.unverified_login(user.email)
    return user

def sendmail_to_signatory(user, pid):
    p = db.select('petition', where='id=$pid', vars=locals())[0]
    p.url = 'http//watchdog.net/c/%s' % (pid)
    token = auth.get_secret_token(user.email)
    msg = render_plain.signatory_mailer(user, p, token)
    #@@@ shouldn't this web.utf8 stuff taken care by in web.py?
    web.sendmail(web.utf8(config.from_address), web.utf8(user.email), web.utf8(msg.subject.strip()), web.utf8(msg))

def is_author(email, pid):
    if not email: return False

    try:
        user_id = db.select('users', where='email=$email', what='id', vars=locals())[0].id
        owner_id = db.select('petition', where='id=$pid', what='owner_id', vars=locals())[0].owner_id
    except:
        return False
    else:
        return user_id == owner_id

def get_signs(pid):
    return db.select(['signatory', 'users'],
                        what='users.fname, users.lname, users.email, '
                              'signatory.share_with, signatory.comment, '
                              'signatory.signed',
                        where='petition_id=$pid AND user_id=users.id',
                        order='signed desc',
                        vars=locals())

class signatories:
    def GET(self, pid):
        user_email = helpers.get_loggedin_email()
        ptitle = db.select('petition', what='title', where='id=$pid', vars=locals())[0].title
        signs = get_signs(pid).list()
        return render.signature_list(pid, ptitle, signs, is_author(user_email, pid))

class petition:
    def GET(self, pid, signform=None):
        i = web.input()

        options = ['unsign', 'edit', 'delete']
        if i.get('m', None) in options:
            handler = getattr(self, 'GET_'+i.m)
            return handler(pid)

        try:
            p = db.select('petition', where='id=$pid', vars=locals())[0]
        except:
            raise web.notfound

        p.signatory_count = db.query('select count(*) from signatory where petition_id=$pid',
                                        vars=locals())[0].count

        if not signform:
            signform = forms.signform()
            fill_user_details(signform, ['name', 'email'])

        msg, msg_type = helpers.get_delete_msg()
        useremail = helpers.get_loggedin_email() or helpers.get_unverified_email()
        isauthor = is_author(useremail, pid)
        return render.petition(p, signform, useremail, isauthor, msg)

    @auth.require_login
    def GET_edit(self, pid):
        user_email = helpers.get_loggedin_email()
        if is_author(user_email, pid):
            p = db.select('petition', where='id=$pid', vars=locals())[0]
            u = helpers.get_user_by_email(user_email)
            pform = forms.petitionform()
            pform.fill(userid=u.id, email=user_email, pid=p.id, ptitle=p.title, msg=p.description)
            cform = forms.wyrform()
            cform.fill(prefix=u.prefix, fname=u.fname, lname=u.lname, addr1=u.addr1,
                        addr2=u.addr2, city=u.city, zipcode=u.zip5, phone=u.phone)
            title = "Edit your petition"
            return render.petitionform(pform, cform, title, target='/c/%s?m=edit' % (pid))
        else:
            login_link = '<a href="/u/login">Login</a>'
            helpers.set_msg('Only author of this petition can edit it. %s if you are.' % login_link, msg_type='error')
            raise web.seeother('/%s' % pid)


    def GET_unsign(self, pid):
        i = web.input()
        user = helpers.get_user_by_email(i.email)

        if user:
            signatory = db.select('signatory', where='petition_id=$pid and user_id=$user.id', vars=locals())

        if not (user and signatory and auth.check_secret_token(i.email, i.token)):
            msg = "Invalid token or there is no signature for this petition with this email."
            msg_type = 'error'
        else:
            msg = render_plain.confirm_unsign(pid, user.id)
            msg_type = ''

        helpers.set_msg(msg, msg_type)
        raise web.seeother('/%s' % pid)

    def GET_delete(self, pid):
        user_email = helpers.get_loggedin_email()
        if is_author(user_email, pid):
            msg = render_plain.confirm_deletion(pid)
            helpers.set_msg(msg)
        else:
            login_link = '<a href="/u/login">Login</a>'
            helpers.set_msg('Only author of this petition can delete it. %s if you are.' % login_link, msg_type='error')

        raise web.seeother('/%s' % pid)

    def POST(self, pid):
        i = web.input('m', _method='GET')
        options = ['sign', 'unsign', 'edit', 'password', 'delete']
        if i.m in options:
            handler = getattr(self, 'POST_'+i.m)
            return handler(pid)
        else:
            raise ValueError

    def POST_password(self, pid):
        form = forms.passwordform()
        i = web.input()
        if form.validates(i):
            save_password(i)
            raise web.seeother('/%s' % pid)
        else:
            return self.GET(pid, passwordform=form)

    def POST_sign(self, pid):
        form = forms.signform()
        i = web.input()
        email = helpers.get_loggedin_email() or helpers.get_unverified_email()
        if email:
            i.email = email
        if form.validates(i):
            user = save_signature(i, pid)
            sendmail_to_signatory(user, pid)
            raise web.seeother('/%s/share' % pid)
        else:
            return self.GET(pid, signform=form)

    @auth.require_login
    def POST_edit(self, pid):
        i = web.input()
        tocongress = i.get('tocongress', 'off') == 'on'
        pform = forms.petitionform()
        pform.inputs = filter(lambda i: i.name != 'pid', pform.inputs)
        wyrform = forms.wyrform()
        wyr_valid = (not(tocongress) or wyrform.validates(i))
        if not pform.validates(i) or not wyr_valid:
            title = "Edit petition"
            return render.petitionform(pform, wyrform, title, target='/c/%s?m=edit' % (pid))
        db.update('petition', where='id=$pid', title=i.ptitle, description=i.msg, vars=locals())
        db.update('users', where='id=$i.userid', prefix=i.prefix, fname=i.fname, lname=i.lname,
                  addr1=i.addr1, addr2=i.addr2, city=i.city, zip5=i.zipcode, phone=i.phone, vars=locals())
        raise web.seeother('/%s' % pid)

    def POST_unsign(self, pid):
        i = web.input()
        now = datetime.now()
        db.update('signatory',
                        deleted=now,
                        where='petition_id=$pid and user_id=$i.user_id',
                        vars=locals())
        msg = 'Your signature has been removed for this petition.'
        helpers.set_msg(msg)
        raise web.seeother('/%s' % pid)

    def POST_delete(self, pid):
        now = datetime.now()
        title = db.select('petition', what='title', where='id=$pid', vars=locals())[0].title
        db.update('petition', where='id=$pid', deleted=now, vars=locals())
        helpers.set_msg('Petition "%s" deleted' % (title))
        raise web.seeother('/')

def get_contacts(user, by='id'):
    if by == 'email':
        where = 'uemail=$user'
    else:
        where = 'user_id=$user'

    contacts = db.select('contacts',
                    what='cname as name, cemail as email, provider',
                    where=where,
                    vars=locals()).list()

    if by == 'id':
        #remove repeated emails due to multiple providers; prefer the one which has name
        cdict = {}
        for c in contacts:
            if c.email not in cdict.keys():
                cdict[c.email] = c
            elif c.name:
                cdict[c.email] = c
        contacts = cdict.values()

    for c in contacts:
        c.name = c.name or c.email.split('@')[0]

    contacts.sort(key=lambda x: x.name.lower())
    return contacts

def signed(email, pid):
    try:
        user_id = db.select('users', what='id', where='email=$email', vars=locals())[0].id
    except IndexError:
        return False
    else:
        is_signatory = db.select('signatory', where='user_id=$user_id and petition_id=$pid', vars=locals())
        return bool(is_signatory)

class share:
    def GET(self, pid, emailform=None, loadcontactsform=None):
        i = web.input()
        user_id = helpers.get_loggedin_userid()
        contacts = get_contacts(user_id)
        if (not contacts) and ('email' in session):
            contacts = get_contacts(session.get('email'), by='email')

        contacts = filter(lambda c: not signed(c.email, pid), contacts)
        petition = db.select('petition', where='id=$pid', vars=locals())[0]
        petition.url = 'http://watchdog.net/c/%s' %(pid)

        if not emailform:
            emailform = forms.emailform
            msg = render_plain.share_petition_mail(petition)
            emailform.fill(subject=petition.title, body=msg)

        msg, msg_type = helpers.get_delete_msg()
        return render.share_petition(petition, emailform,
                            contacts, loadcontactsform, msg)

    def POST(self, pid):
        i = web.input()
        emailform = forms.emailform()
        if emailform.validates(i):
            pid, msg, subject = i.pid, i.body, i.subject
            emails = [e.strip() for e in i.emails.strip(', ').split(',')]
            web.sendmail(config.from_address, emails, subject, msg)
            helpers.set_msg('Thanks for sharing this petition with your friends!')
            raise web.seeother('/%s' % (pid))
        else:
            return self.GET(pid, emailform=emailform)


app = web.application(urls, globals())

if __name__ == '__main__':
    app.run()
