/*
  
Analysis UX Flow:
  
User enters URL and clicks "Analyze" ->
Perform search for existing results and show (Re)submit button ->
Request gets resubmitted, page polls for updates ->
page notifies user as soon as results are present
  
*/
(function($){
  "use strict";
  /*jshint browser: true, devel: true, debug: true, evil: true, forin: false, undef: true, bitwise: true, eqnull: true, noarg: true, noempty: true, eqeqeq: true, boss: true, loopfunc: true, laxbreak: true, strict: true, curly: false, nonew: false, jquery: true */
  /* global _, buzz, Recaptcha */

  var exports = window;
  
  var notifySound = new buzz.sound("/static/sounds/airhorn", {
    formats: [ "ogg", "mp3" ],
    preload: false,
    autoplay: false,
    loop: false
  }); 
  
  function replaceState(url){
    history.pushState({}, "", url);
  }

  var TemplateMixin = {
    initialize: function(args){
      this._initialize.apply(this,args);
    },
    _initialize: function(node, options){
      this.$parentNode = $(node);
      this.options = $.extend({}, this.defaults, options);
      this.render();
    },
    postRender: function(){},
    render: function(){
      if(!this._template){
        console.debug("Rendering template",this);
        var templatesrc = this.template.indexOf("#") === 0 ? $(this.template).html() : this.template;
        Object.getPrototypeOf(this)._template = _.template($.trim(templatesrc));
      }
      var $e = $($.parseHTML(this._template(this))[0]);
      if(this.$element)
        this.$element.replaceWith($e);
      this.$element = $e;
      this._attachNodes();
      this.postRender();
    },
    _attachNodes: function(){
      var self = this;
      this.$element.find("[data-attach]").each(function(){
        var attr = $(this).data("attach");
        self[    attr] =   this ;
        self["$"+attr] = $(this);
      });
    }
  };
  
  var MainView = exports.MainView = function(node,options){
    this.noResults = _.template($("#maintpl-noresults").html());
    this.row = _.template($("#maintpl-row").html());
    
    this.initialize(arguments);
    
    this.options.startRows = this.options.rows;
    
    this.$parentNode.append(this.$element);
    
    this.submitHandler = new SubmitHandler(this.$parentNode);
    
  };
  $.extend(MainView.prototype,TemplateMixin,{
    template: "#maintpl",
    defaults: {rows: []},
    postRender: function(){
      this.$analyzeForm.submit(this.search.bind(this));
      this.$analyzeUrl.focus();
      if(this.submitHandler)
        this.submitHandler.setUrl(this.options.url, this.options.rows.length > 0);
    },
    search: function(){
      this.options.url = this.$analyzeUrl.val().trim();

      this.$analyzeButtonIcon.removeClass("icon-spin");

      if(this.options.url=== ""){
        this.options.rows = this.options.startRows;
        this.render();
      } else {
        //Search for existing results
        this.$analyzeButtonIcon.addClass("icon-spin");
        if(this.searchrequest)
          this.searchrequest.abort();
        this.searchrequest = $.post("/api/search", this.$analyzeForm.serialize(), "json").always((function(data){
          this.$analyzeButtonIcon.removeClass("icon-spin");
        
          this.options.rows = (data && data.results) ? data.results : [];
          this.render();
          
        }).bind(this));
      }

      return false;
    }
  });
  
  //We don't use the usual rerendering for the Submithandler as it doesn't work with Recaptcha.
  var SubmitHandler = function(node,options){
    this.initialize(arguments);

    this.$element.submit(this.submit.bind(this));
    
    this.$element.hide();
    this.$parentNode.append(this.$element);

  };
  $.extend(SubmitHandler.prototype, TemplateMixin, {
    template: "#submittpl",
    defaults:  {url:"http://example.com/", hasResults: false},
    initCaptcha: function(){
      //Lazy-load Recaptcha
      
      var showRecaptcha = (function(){
        Recaptcha.create("6LcYzt0SAAAAAMG60o7oeNa9AZ_BYz0fAgc64Pu4", //FIXME: insert correct key
                         this.captcha, {
                           theme: "white",
                           callback: Recaptcha.focus_response_field
                         });
      }).bind(this);
      
      if(!window.lazyRecaptchaInit){
        $.getScript("//www.google.com/recaptcha/api/js/recaptcha_ajax.js").then(showRecaptcha);
        window.lazyRecaptchaInit = true;
      }
    },
    setUrl: function(url, hasResults){
      this.$urlInput.val(url.trim());
      this.$title.text(hasResults ? "Reanalyze!" : "Analyze!");
      this.options.hasResults = hasResults;
      if(/^(https?:\/\/)?[a-zA-Z0-9_\-\.]+\.[a-zA-Z0-9]+(:\d+)?(\/.*)?$/.test(url)) {
        this.$element.fadeIn("fast");
        this.initCaptcha();
        if(window.Recaptcha)
          window.Recaptcha.focus_response_field();
        return true;
      } else {
        this.$element.fadeOut("fast");
        return false;
      }
    },
    submit: function(){
      if(!window.Recaptcha) {
        alert("Recaptcha has not been loaded yet.");
      }
      else {
        $.post("/api/analyze", this.$element.serialize(), "json").then((function(data){
          if(!data.success) {
            this.$captchaError.slideDown("fast");
            Recaptcha.reload();
          } else {
            this.$parentNode.children().slideUp().promise().done(function(){
              $(this).remove();
            });
            new QueueHandler(this.$parentNode, {}, data);
          }
        }).bind(this));
      }
      return false;
    }
  });

  var ShareWidget = exports.ShareWidget = function(node,options){
    this.initialize(arguments);

    this.$parentNode.append(this.$element);

    this.$element.on("click",".share-open",function(){
      window.open(this.href,'', 'menubar=no,toolbar=no,resizable=yes,scrollbars=yes,height=600,width=600');
      return false;
    });
    
    this.$urlCopyInput
      .click(function(){
        $(this).select();
      })
      .tooltip();
  };
  $.extend(ShareWidget.prototype,TemplateMixin, {
    template: "#sharetpl",
    defaults: {text: "Share:", url: "http://example.com"}
  });
  
  var QueueHandler = exports.QueueHandler = function(node,options,firstData){
    this.initialize(arguments);
    this.id = firstData ? firstData.id || options.id : options.id;
    
    this.initGui();
    
    
    if(firstData)
      this.handleStatus(firstData);
    else
      this.pollStatus();
    
    this.pollStatusInterval = window.setInterval(this.pollStatus.bind(this),3000);
  };
  $.extend(QueueHandler.prototype, TemplateMixin, {
    template: "#queuetpl",
    defaults: { showSuccessMessage: false },
    initGui: function(){
      var url = "/analysis/"+this.id;
      //hide existing content
    
      new ShareWidget(this.$shareWidget,
                      {text: "Share analysis:",
                       url: location.host + url});
    
      this.$notifyButton.click(this.notifyClick.bind(this));
    
      this.$element.hide();
      this.$parentNode.append(this.$element);
      this.$element.slideDown();
      
      notifySound.load();
      replaceState(url);
    },
    pollStatus: function(){
      console.log("Poll Status for "+this.id+"...");
      $.get("/api/analysis/"+this.id, this.handleStatus.bind(this),"json");
    },
    handleStatus: function(data){
      if(data.complete || data.queue < 0) {
        window.clearInterval(this.pollStatusInterval);
        this.notify().done(function(){
          location.reload();
        });
      } else {
        this.setQueueNumber(data.queue);
      }
    },
    setQueueNumber: function(no){
      window.document.title = "#"+no+" in queue - Honeyproxy Client";
      this.$queuePosition.text(no);
    },
    notify: function(){
      var def = $.Deferred();
      if(!this.$notifyButton.hasClass("active"))
        def.resolve(false);
      else {
        notifySound.stop();
        notifySound.play();
        notifySound.bindOnce("ended", function() {
          def.resolve(true);
        });
      }
      return def;
    },
    notifyClick: function(){
      //TODO: Add support for HTML5 Notifications API as soon as Chrome supports the new spec
      // http://www.html5rocks.com/en/tutorials/notifications/quick/
      if(!this.$notifyButton.hasClass("active")) { //activated
        notifySound.play();
      }
    }
  });
  
})(jQuery);